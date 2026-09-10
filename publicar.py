"""Publica Reels vencidos no Instagram e Facebook sob a política PQD."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, MutableMapping

from automacao_comum import (
    BRT,
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
    validar_preflight_meta,
)
from politica_agenda import (
    carregar_politica,
    limitar_reels_pelo_teto_real,
    validar_coerencia_ambiente,
    validar_fila_reels,
    validar_manual_nao_futuro,
)


ROOT = Path(__file__).resolve().parent
FILA_FILE = ROOT / "fila" / "fila-reels.json"
LEGENDA_REELS = "siga @palavraquedesperta_br"
LIMITE_POR_EXECUCAO = 10


def itens_devidos(
    fila: dict,
    agora: datetime | None = None,
    limite: int = LIMITE_POR_EXECUCAO,
    data_forcada: str = "",
    horario_forcado: str = "",
) -> list[MutableMapping[str, object]]:
    if limite < 1 or limite > LIMITE_POR_EXECUCAO:
        raise RuntimeError(f"O limite por execução deve ficar entre 1 e {LIMITE_POR_EXECUCAO}.")
    if bool(data_forcada) != bool(horario_forcado):
        raise RuntimeError("Para execução manual, informe data e horário juntos.")

    candidatos: list[MutableMapping[str, object]] = []
    agora = agora or agora_brt()
    if data_forcada:
        try:
            momento_forcado = datetime.fromisoformat(
                f"{data_forcada}T{horario_forcado}:00"
            ).replace(tzinfo=BRT)
        except ValueError:
            raise RuntimeError("Data/horário manual inválido.") from None
        validar_manual_nao_futuro(momento_forcado, agora)
    for item in fila.get("conteudos", []):
        if not isinstance(item, MutableMapping) or item.get("status") == "concluido":
            continue
        if data_forcada:
            if item.get("data") == data_forcada and item.get("horario") == horario_forcado:
                candidatos.append(item)
            continue
        if momento_item(item, "09:00") <= agora:
            candidatos.append(item)

    candidatos.sort(key=lambda item: (momento_item(item, "09:00"), str(item.get("id", ""))))
    if data_forcada and len(candidatos) > 1:
        raise RuntimeError("A fila contém mais de um Reel para a data e o horário informados.")
    return candidatos[:limite]


def publicar_instagram_reel(
    item: MutableMapping[str, object],
    cliente: MetaClient,
    checkpoint: Callable[[], None],
) -> str:
    token = obrigatoria("PQD_IG_ACCESS_TOKEN")
    ig_id = obrigatoria("PQD_IG_BUSINESS_ID")
    registro = item["instagram"]
    assert isinstance(registro, MutableMapping)
    midia = item.get("midia", {})
    if not isinstance(midia, MutableMapping) or not midia.get("url_publica"):
        raise RuntimeError("O Reel não possui URL pública de mídia.")

    container_id = str(registro.get("container_id", "")).strip()
    if not container_id:
        resposta = cliente.post(
            f"{ig_id}/media",
            {
                "media_type": "REELS",
                "video_url": str(midia["url_publica"]),
                "caption": LEGENDA_REELS,
                "share_to_feed": "true",
                "access_token": token,
            },
        )
        container_id = str(resposta.get("id", "")).strip()
        if not container_id:
            raise RuntimeError("O Instagram não retornou o ID do container do Reel.")
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


def _video_facebook_pronto(dados: dict) -> bool:
    status = dados.get("status", {})
    if isinstance(status, dict):
        valor = str(status.get("video_status", "")).casefold()
    else:
        valor = str(status).casefold()
    return valor in {"ready", "published", "complete", "completed"}


def _aguardar_video_facebook_pronto(
    cliente: MetaClient,
    video_id: str,
    token: str,
    tentativas: int = 12,
    intervalo: float = 5.0,
) -> bool:
    """Só considera o upload retomável após status terminal de prontidão."""
    for tentativa in range(tentativas):
        dados = cliente.get(video_id, {"fields": "id,status", "access_token": token})
        if _video_facebook_pronto(dados):
            return True
        if tentativa + 1 < tentativas:
            time.sleep(intervalo)
    return False


def publicar_facebook_reel(
    item: MutableMapping[str, object],
    cliente: MetaClient,
    checkpoint: Callable[[], None],
    caminho_validado: Path | None = None,
) -> str:
    token = obrigatoria("PQD_FB_PAGE_ACCESS_TOKEN")
    page_id = obrigatoria("PQD_FB_PAGE_ID")
    registro = item["facebook"]
    assert isinstance(registro, MutableMapping)
    midia = item.get("midia", {})
    if not isinstance(midia, MutableMapping):
        raise RuntimeError("Metadados do Reel ausentes.")

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
            # A sessão incompleta não publicou nada. Abandone-a sem guardar URL
            # assinada e inicie uma sessão limpa.
            registro.pop("video_id", None)
            registro.pop("upload_concluido", None)
            video_id = ""
            checkpoint()

    if not upload_concluido:
        inicio = cliente.post(
            f"{page_id}/video_reels",
            {"upload_phase": "start", "access_token": token},
        )
        video_id = str(inicio.get("video_id", "")).strip()
        upload_url = str(inicio.get("upload_url", "")).strip()
        if not video_id or not upload_url:
            raise RuntimeError("O Facebook não iniciou o upload do Reel.")
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
        f"{page_id}/video_reels",
        {
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": LEGENDA_REELS,
            "access_token": token,
        },
    )
    if not fim.get("success"):
        raise RuntimeError("O Facebook não confirmou a publicação do Reel.")
    return video_id


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
            "Publicação de Reels",
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
        validar_fila_reels(fila, politica_execucao)
        validar_coerencia_ambiente(politica_execucao)
    try:
        limite = int(os.getenv("PQD_MAX_REELS_POR_EXECUCAO", str(LIMITE_POR_EXECUCAO)))
    except ValueError as erro:
        raise RuntimeError("PQD_MAX_REELS_POR_EXECUCAO deve ser inteiro.") from erro
    limite = min(limite, LIMITE_POR_EXECUCAO)
    momento_execucao = agora or agora_brt()
    selecionados = itens_devidos(
        fila,
        agora=momento_execucao,
        limite=limite,
        data_forcada=os.getenv("PQD_DATA_PUBLICACAO", "").strip(),
        horario_forcado=os.getenv("PQD_HORARIO_PUBLICACAO", "").strip(),
    )
    teto_real: dict[str, int] | None = None
    if politica_execucao is not None:
        selecionados, teto_real = limitar_reels_pelo_teto_real(
            selecionados, fila, politica_execucao, momento_execucao
        )
    if not selecionados:
        linhas = ["0 Reels processados."]
        if teto_real:
            linhas.append(
                "Teto real BRT: "
                f"{teto_real['limite']}/rede; usados IG={teto_real['usados_instagram']}, "
                f"FB={teto_real['usados_facebook']}."
            )
        emitir_resumo("SEM_ITENS_DEVIDOS_OU_TETO", "Publicação de Reels", tuple(linhas))
        return 0

    cliente = cliente or MetaClient()
    if not preflight_realizado:
        validar_preflight_meta(cliente)

    publicador_instagram = publicador_instagram or (
        lambda item, checkpoint: publicar_instagram_reel(item, cliente, checkpoint)
    )
    validador_midia = validador_midia or (
        lambda midia: baixar_midia(midia, sessao=cliente.sessao)
    )
    publicados = erros = examinados = 0
    detalhes: list[str] = []
    for item in selecionados:
        examinados += 1
        persistir = criar_persistidor(fila_path, fila)
        identificador_item = str(item.get("id", "sem-id"))
        if item_concluido(item):
            item.update({"status": "concluido", "concluido_em": agora_brt().isoformat()})
            persistir("reel:conclusao_recuperada")
            publicados += 1
            detalhes.append(
                f"PUBLICADO {identificador_item}: instagram={item['instagram']['id']}; "
                f"facebook={item['facebook']['id']}"
            )
            continue
        caminho_validado: Path | None = None
        try:
            midia = item.get("midia", {})
            if not isinstance(midia, MutableMapping):
                raise RuntimeError("Metadados do Reel ausentes.")
            # Preflight obrigatório: a URL só chega ao Instagram depois que o
            # mesmo asset foi baixado e conferido por SHA-256 e tamanho.
            caminho_validado = Path(validador_midia(midia))
        except Exception as erro_original:
            erro = mensagem_segura(erro_original)
            item.update(
                {
                    "status": "erro_midia",
                    "erro_midia": erro,
                    "atualizado_em": agora_brt().isoformat(),
                }
            )
            persistir("reel:preflight_midia_falhou")
            erros += 1
            detalhes.append(f"ERRO {identificador_item}: preflight de mídia falhou: {erro}")
            break
        publicador_fb_item = publicador_facebook or (
            lambda atual, checkpoint, caminho=caminho_validado: publicar_facebook_reel(
                atual, cliente, checkpoint, caminho
            )
        )
        try:
            resultado_instagram = executar_plataforma(
                item, "instagram", publicador_instagram, persistir
            )
            if resultado_instagram.resultado == "erro":
                resultados = (resultado_instagram,)
            else:
                resultados = (
                    resultado_instagram,
                    executar_plataforma(item, "facebook", publicador_fb_item, persistir),
                )
        finally:
            caminho_validado.unlink(missing_ok=True)
        if item_concluido(item):
            item.update({"status": "concluido", "concluido_em": agora_brt().isoformat()})
            persistir("reel:concluido_duas_redes")
            publicados += 1
            detalhes.append(
                f"PUBLICADO {identificador_item}: instagram={item['instagram']['id']}; "
                f"facebook={item['facebook']['id']}"
            )
        if any(resultado.resultado == "erro" for resultado in resultados):
            erros += 1
            detalhes.append(
                f"ERRO {identificador_item}: instagram={item['instagram'].get('status')}; "
                f"facebook={item['facebook'].get('status')}"
            )
            # Preserva a ordem global: nenhum Reel posterior atravessa a falha.
            break

    resultado_final = "ERRO" if erros else "PUBLICADO"
    emitir_resumo(
        resultado_final,
        "Publicação de Reels",
        (
            f"{examinados} Reels examinados (limite absoluto: {LIMITE_POR_EXECUCAO}).",
            f"{publicados} Reels confirmados nas duas redes.",
            f"{erros} Reels com ao menos uma rede em erro.",
            *detalhes,
        ),
    )
    return 1 if erros else 0


def main() -> None:
    try:
        codigo = executar()
    except Exception as erro:
        emitir_resumo("ERRO", "Publicação de Reels", (mensagem_segura(erro),))
        raise
    raise SystemExit(codigo)


if __name__ == "__main__":
    main()
