"""Publica, em ordem, no máximo um pacote vencido de Stories."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, MutableMapping

from automacao_comum import (
    token_pagina_facebook,
    MetaClient,
    MetaMidiaTerminalErro,
    agora_brt,
    automacao_ativa,
    baixar_midia,
    carregar_json,
    criar_persistidor,
    emitir_resumo,
    exigir_persistencia_duravel,
    executar_plataforma,
    item_concluido,
    mensagem_segura,
    mensagem_travas,
    momento_item,
    obrigatoria,
    plataforma_publicada,
    validar_preflight_meta,
)
from politica_agenda import (
    carregar_politica,
    story_pode_iniciar_no_dia_real,
    validar_coerencia_ambiente,
    validar_fila_stories,
    validar_manual_nao_futuro,
)
from publicar import _aguardar_video_facebook_pronto


ROOT = Path(__file__).resolve().parent
FILA_FILE = ROOT / "fila" / "fila-stories.json"
LIMITE_PARTE_SEGUNDOS = 59.0


@dataclass(frozen=True)
class CotaInstagram:
    confiavel: bool
    suficiente: bool
    necessarias: int
    restantes: int | None
    mensagem: str


def pacotes_devidos(
    fila: dict,
    agora: datetime | None = None,
    data_forcada: str = "",
) -> list[MutableMapping[str, object]]:
    agora = agora or agora_brt()
    if data_forcada:
        try:
            momento_forcado = datetime.fromisoformat(f"{data_forcada}T09:00:00").replace(
                tzinfo=agora.tzinfo
            )
        except ValueError:
            raise RuntimeError("Data manual inválida.") from None
        validar_manual_nao_futuro(momento_forcado, agora)
    candidatos: list[MutableMapping[str, object]] = []
    for pacote in fila.get("pacotes", []):
        if not isinstance(pacote, MutableMapping) or pacote.get("status") == "concluido":
            continue
        if data_forcada:
            if pacote.get("data") == data_forcada:
                candidatos.append(pacote)
        elif momento_item(pacote, "09:00") <= agora:
            candidatos.append(pacote)
    candidatos.sort(key=lambda pacote: (momento_item(pacote, "09:00"), str(pacote.get("id", ""))))
    if data_forcada and len(candidatos) > 1:
        raise RuntimeError("A fila contém mais de um pacote de Stories para a data informada.")
    return candidatos[:1]


def partes_ordenadas(pacote: MutableMapping[str, object]) -> list[MutableMapping[str, object]]:
    partes = pacote.get("partes", [])
    if not isinstance(partes, list) or not partes:
        raise RuntimeError("O pacote de Stories não possui partes.")
    if not all(isinstance(parte, MutableMapping) for parte in partes):
        raise RuntimeError("O pacote de Stories possui uma parte inválida.")
    ordenadas = sorted(partes, key=lambda parte: int(parte.get("ordem", 0)))
    ordens = [int(parte.get("ordem", 0)) for parte in ordenadas]
    if ordens != list(range(1, len(ordenadas) + 1)):
        raise RuntimeError("As partes do pacote precisam ter ordens contíguas a partir de 1.")
    for parte in ordenadas:
        midia = parte.get("midia", {})
        if not isinstance(midia, MutableMapping):
            raise RuntimeError(f"Parte {parte['ordem']} sem metadados de mídia.")
        try:
            duracao = float(midia.get("duracao_segundos", 0))
        except (TypeError, ValueError) as erro:
            raise RuntimeError(f"Duração inválida na parte {parte['ordem']}.") from erro
        if duracao <= 0 or duracao > LIMITE_PARTE_SEGUNDOS:
            raise RuntimeError(
                f"Parte {parte['ordem']} tem {duracao:.3f}s; o limite estrito é {LIMITE_PARTE_SEGUNDOS:.0f}s."
            )
    return ordenadas


def consultar_cota_instagram(
    cliente: MetaClient,
    ig_id: str,
    token: str,
    necessarias: int,
) -> CotaInstagram:
    if necessarias <= 0:
        return CotaInstagram(True, True, 0, None, "Nenhuma nova parte do Instagram é necessária.")
    try:
        resposta = cliente.get(
            f"{ig_id}/content_publishing_limit",
            {"fields": "quota_usage,config", "access_token": token},
        )
    except Exception as erro:
        return CotaInstagram(
            False,
            True,
            necessarias,
            None,
            f"Consulta de cota indisponível; processamento continuará: {erro}",
        )

    registro = resposta
    if isinstance(resposta.get("data"), list) and resposta["data"]:
        primeiro = resposta["data"][0]
        if isinstance(primeiro, dict):
            registro = primeiro
    config = registro.get("config", {}) if isinstance(registro, dict) else {}
    if not isinstance(config, dict):
        config = {}
    try:
        uso = int(registro.get("quota_usage"))
        total = int(config.get("quota_total"))
    except (TypeError, ValueError):
        return CotaInstagram(
            False,
            True,
            necessarias,
            None,
            "A Meta não retornou quota_usage/quota_total confiáveis; processamento continuará.",
        )
    if uso < 0 or total <= 0 or uso > total:
        return CotaInstagram(
            False,
            True,
            necessarias,
            None,
            "A Meta retornou valores de cota inconsistentes; processamento continuará.",
        )
    restantes = total - uso
    return CotaInstagram(
        True,
        restantes >= necessarias,
        necessarias,
        restantes,
        f"Cota Instagram: {restantes} criações restantes; {necessarias} necessárias.",
    )


def publicar_instagram_story(
    parte: MutableMapping[str, object],
    cliente: MetaClient,
    checkpoint: Callable[[], None],
) -> str:
    token = obrigatoria("PQD_IG_ACCESS_TOKEN")
    ig_id = obrigatoria("PQD_IG_BUSINESS_ID")
    registro = parte["instagram"]
    assert isinstance(registro, MutableMapping)
    midia = parte.get("midia", {})
    if not isinstance(midia, MutableMapping) or not midia.get("url_publica"):
        raise RuntimeError("A parte do Story não possui URL pública de mídia.")

    container_id = str(registro.get("container_id", "")).strip()
    if not container_id:
        resposta = cliente.post(
            f"{ig_id}/media",
            {
                "media_type": "STORIES",
                "video_url": str(midia["url_publica"]),
                "access_token": token,
            },
        )
        container_id = str(resposta.get("id", "")).strip()
        if not container_id:
            raise RuntimeError("O Instagram não retornou o ID do container do Story.")
        registro["container_id"] = container_id
        checkpoint()
    try:
        cliente.aguardar_instagram(container_id, token)
    except MetaMidiaTerminalErro:
        registro.pop("container_id", None)
        checkpoint()
        raise
    publicado = cliente.post(
        f"{ig_id}/media_publish",
        {"creation_id": container_id, "access_token": token},
    )
    return str(publicado.get("id", ""))


def publicar_facebook_story(
    parte: MutableMapping[str, object],
    cliente: MetaClient,
    checkpoint: Callable[[], None],
    caminho_validado: Path | None = None,
) -> str:
    token = token_pagina_facebook(cliente)
    page_id = obrigatoria("PQD_FB_PAGE_ID")
    registro = parte["facebook"]
    assert isinstance(registro, MutableMapping)
    midia = parte.get("midia", {})
    if not isinstance(midia, MutableMapping):
        raise RuntimeError("Metadados da parte do Story ausentes.")

    video_id = str(registro.get("video_id", "")).strip()
    upload_concluido = bool(registro.get("upload_concluido"))
    if video_id and not upload_concluido:
        try:
            upload_concluido = _aguardar_video_facebook_pronto(
                cliente,
                video_id,
                token,
                tentativas=int(os.getenv("PQD_FB_STATUS_TENTATIVAS", "12")),
                intervalo=float(os.getenv("PQD_FB_STATUS_INTERVALO", "5")),
            )
        except Exception:
            upload_concluido = False
        if upload_concluido:
            registro["upload_concluido"] = True
            checkpoint()
        else:
            registro.pop("video_id", None)
            registro.pop("upload_concluido", None)
            video_id = ""
            checkpoint()

    if not upload_concluido:
        inicio = cliente.post(
            f"{page_id}/video_stories",
            {"upload_phase": "start", "access_token": token},
        )
        video_id = str(inicio.get("video_id", "")).strip()
        upload_url = str(inicio.get("upload_url", "")).strip()
        if not video_id or not upload_url:
            raise RuntimeError("O Facebook não iniciou o upload do Story.")
        registro["video_id"] = video_id
        checkpoint()
        caminho = caminho_validado or baixar_midia(midia, sessao=cliente.sessao)
        remover_ao_final = caminho_validado is None
        try:
            cliente.upload(upload_url, token, caminho)
        finally:
            if remover_ao_final:
                caminho.unlink(missing_ok=True)
        registro["upload_concluido"] = True
        checkpoint()

    fim = cliente.post(
        f"{page_id}/video_stories",
        {
            "upload_phase": "finish",
            "video_id": video_id,
            "access_token": token,
        },
    )
    if not fim.get("success") or not fim.get("post_id"):
        raise RuntimeError("O Facebook não confirmou a publicação do Story.")
    return str(fim["post_id"])


def executar(
    fila_path: Path = FILA_FILE,
    cliente: MetaClient | None = None,
    agora: datetime | None = None,
    publicador_instagram=None,
    publicador_facebook=None,
    validador_midia=None,
    preflight_realizado: bool = False,
    politica_validada: bool = False,
    requisitos_ativacao_validados: bool = False,
    politica_execucao: dict | None = None,
) -> int:
    if not automacao_ativa():
        emitir_resumo(
            "DESATIVADO",
            "Publicação de Stories",
            (mensagem_travas(),),
        )
        return 0

    if not requisitos_ativacao_validados:
        exigir_persistencia_duravel()
    if politica_validada and politica_execucao is None:
        raise RuntimeError(
            "politica_validada=True exige politica_execucao; teto diário não pode ser ignorado."
        )
    fila = carregar_json(fila_path)
    if not politica_validada:
        politica_execucao = carregar_politica()
        validar_fila_stories(fila, politica_execucao)
        validar_coerencia_ambiente(politica_execucao)
    momento_execucao = agora or agora_brt()
    selecionados = pacotes_devidos(
        fila,
        agora=momento_execucao,
        data_forcada=os.getenv("PQD_DATA_PUBLICACAO", "").strip(),
    )
    if not selecionados:
        emitir_resumo("SEM_ITENS_DEVIDOS", "Publicação de Stories", ("0 pacotes processados.",))
        return 0

    pacote = selecionados[0]
    if politica_execucao is not None and not story_pode_iniciar_no_dia_real(
        pacote, fila, politica_execucao, momento_execucao
    ):
        emitir_resumo(
            "TETO_DIARIO_ATINGIDO",
            "Publicação de Stories",
            ("Um pacote-fonte diferente já foi iniciado hoje em America/Sao_Paulo.",),
        )
        return 0

    cliente = cliente or MetaClient()
    if not preflight_realizado:
        validar_preflight_meta(cliente)
    persistir = criar_persistidor(fila_path, fila)
    try:
        partes = partes_ordenadas(pacote)
    except Exception as erro_original:
        erro = mensagem_segura(erro_original)
        pacote.update(
            {"status": "erro_validacao", "erro": str(erro), "atualizado_em": agora_brt().isoformat()}
        )
        persistir("stories:validacao_falhou")
        emitir_resumo("ERRO", "Publicação de Stories", (str(erro), "Nenhuma parte foi enviada."))
        return 1

    validador_midia = validador_midia or (
        lambda midia: baixar_midia(midia, sessao=cliente.sessao)
    )

    # Preflight integral do pacote. Nenhuma chamada Meta (nem mesmo a consulta
    # de cota) acontece antes de todas as partes ainda necessárias conferirem.
    caches: dict[int, Path] = {}
    try:
        for parte in partes:
            if item_concluido(parte):
                continue
            midia = parte.get("midia", {})
            if not isinstance(midia, MutableMapping):
                raise RuntimeError(f"Parte {parte['ordem']} sem metadados de mídia.")
            caches[int(parte["ordem"])] = Path(validador_midia(midia))
    except Exception as erro_original:
        erro = mensagem_segura(erro_original)
        for caminho in caches.values():
            caminho.unlink(missing_ok=True)
        pacote.update(
            {
                "status": "erro_midia",
                "erro_midia": str(erro),
                "atualizado_em": agora_brt().isoformat(),
            }
        )
        persistir("stories:preflight_midia_falhou")
        emitir_resumo(
            "ERRO",
            "Publicação de Stories",
            (f"Preflight integral falhou: {erro}", "Nenhuma parte foi enviada."),
        )
        return 1

    try:
        ig_id, ig_token = obrigatoria("PQD_IG_BUSINESS_ID"), obrigatoria("PQD_IG_ACCESS_TOKEN")
        necessarias = sum(
            1
            for parte in partes
            if not plataforma_publicada(parte.get("instagram", {}))  # type: ignore[arg-type]
        )
        cota = consultar_cota_instagram(cliente, ig_id, ig_token, necessarias)
        pacote["ultima_consulta_cota_instagram"] = {
            "consultado_em": agora_brt().isoformat(),
            "confiavel": cota.confiavel,
            "necessarias": cota.necessarias,
            "restantes": cota.restantes,
            "mensagem": cota.mensagem,
        }
        persistir("stories:cota_consultada")
        if cota.confiavel and not cota.suficiente:
            pacote.update({"status": "aguardando_cota", "atualizado_em": agora_brt().isoformat()})
            persistir("stories:aguardando_cota")
            emitir_resumo(
                "AGUARDANDO_COTA",
                "Publicação de Stories",
                (cota.mensagem, "Nenhuma parte do pacote foi enviada."),
            )
            return 0

        publicador_instagram = publicador_instagram or (
            lambda parte, checkpoint: publicar_instagram_story(parte, cliente, checkpoint)
        )

        if pacote.get("status") not in {"publicacao_iniciada", "revisao_manual"}:
            pacote.update(
                {
                    "status": "publicacao_iniciada",
                    "publicacao_iniciada_em": agora_brt().isoformat(),
                }
            )
            persistir("stories:pacote_antes_da_primeira_mutacao")

        partes_concluidas = erros = 0
        ids_novos: list[str] = []
        for parte in partes:
            if item_concluido(parte):
                parte["status"] = "concluido"
                partes_concluidas += 1
                continue
            antes = {
                plataforma: plataforma_publicada(parte.get(plataforma, {}))  # type: ignore[arg-type]
                for plataforma in ("instagram", "facebook")
            }
            caminho = caches[int(parte["ordem"])]
            publicador_fb_parte = publicador_facebook or (
                lambda atual, checkpoint, validado=caminho: publicar_facebook_story(
                    atual, cliente, checkpoint, validado
                )
            )
            resultado_instagram = executar_plataforma(
                parte, "instagram", publicador_instagram, persistir
            )
            if resultado_instagram.resultado == "erro":
                resultados = (resultado_instagram,)
            else:
                resultados = (
                    resultado_instagram,
                    executar_plataforma(parte, "facebook", publicador_fb_parte, persistir),
                )
            for plataforma in ("instagram", "facebook"):
                registro = parte.get(plataforma, {})
                if not antes[plataforma] and plataforma_publicada(registro):  # type: ignore[arg-type]
                    ids_novos.append(
                        f"PUBLICADO pacote={pacote.get('id', 'sem-id')} parte={parte['ordem']} "
                        f"rede={plataforma} id={registro['id']}"  # type: ignore[index]
                    )
            if item_concluido(parte):
                parte.update({"status": "concluido", "concluido_em": agora_brt().isoformat()})
                persistir("stories:parte_concluida_duas_redes")
                partes_concluidas += 1
            if any(resultado.resultado == "erro" for resultado in resultados):
                erros += 1
                pacote.update(
                    {
                        "status": "revisao_manual",
                        "erro": "Uma parte ficou em estado ambíguo; reenvio automático bloqueado.",
                        "atualizado_em": agora_brt().isoformat(),
                    }
                )
                persistir("stories:pacote_revisao_manual")
                # Nunca atravesse uma falha: a próxima execução retoma esta parte.
                break

        pacote_concluido = all(item_concluido(parte) for parte in partes)
        if pacote_concluido:
            pacote.update({"status": "concluido", "concluido_em": agora_brt().isoformat()})
            pacote.pop("erro", None)
            persistir("stories:pacote_concluido_duas_redes")

        resultado_final = "ERRO" if erros else "PUBLICADO"
        linhas = [
            "1 pacote examinado (limite absoluto por execução).",
            f"{partes_concluidas}/{len(partes)} partes confirmadas nas duas redes.",
            f"Pacote concluído: {'sim' if pacote_concluido else 'não'}.",
            cota.mensagem,
            *ids_novos,
        ]
        if not cota.confiavel:
            linhas.append("A ausência de cota confiável foi registrada; nenhum teto foi presumido.")
        emitir_resumo(resultado_final, "Publicação de Stories", tuple(linhas))
        return 1 if erros else 0
    finally:
        for caminho in caches.values():
            caminho.unlink(missing_ok=True)


def main() -> None:
    try:
        codigo = executar()
    except Exception as erro:
        emitir_resumo("ERRO", "Publicação de Stories", (mensagem_segura(erro),))
        raise
    raise SystemExit(codigo)


if __name__ == "__main__":
    main()
