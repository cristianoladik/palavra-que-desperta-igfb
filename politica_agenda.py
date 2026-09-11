"""Política versionada e independente para validar filas antes da publicação."""

from __future__ import annotations

from collections import Counter
import math
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Mapping, MutableMapping
from urllib.parse import quote

from automacao_comum import BRT, ROOT, carregar_json, momento_item


POLITICA_FILE = ROOT / "politica_publicacao.json"
HORARIOS_REELS_APROVADOS = {
    "1": ["09:00"],
    "2": ["09:00", "21:00"],
    "3": ["05:00", "09:00", "21:00"],
    "4": ["05:00", "09:00", "13:00", "21:00"],
    "5+": ["05:00", "09:00", "13:00", "17:00", "21:00"],
}
LIMITES_REELS_APROVADOS = {"1": 1, "2": 2, "3": 3, "4": 4, "5+": 5}
TOLERANCIA_IGUALDADE_STORY_SEGUNDOS = 0.25


class PoliticaErro(RuntimeError):
    pass


def carregar_politica(caminho: Path = POLITICA_FILE) -> dict:
    politica = carregar_json(caminho)
    if politica.get("versao") != 1:
        raise PoliticaErro("Versão da política de publicação não suportada.")
    if politica.get("publicacao_habilitada") is not True:
        raise PoliticaErro(
            "publicacao_habilitada=false; a política versionada bloqueia a ativação."
        )
    if politica.get("autoridade_unica_confirmada") is not True:
        raise PoliticaErro(
            "autoridade_unica_confirmada=false; confirme que não existe outro publicador da conta."
        )
    if politica.get("timezone") != "America/Sao_Paulo":
        raise PoliticaErro("A política precisa usar America/Sao_Paulo.")
    if politica.get("instagram_username") != "palavraquedesperta_br":
        raise PoliticaErro("instagram_username diverge da identidade fixa do projeto.")
    if politica.get("legenda_reels") != "siga @palavraquedesperta_br":
        raise PoliticaErro("legenda_reels diverge da legenda aprovada.")
    if politica.get("meta_graph_version") != "v26.0":
        raise PoliticaErro("meta_graph_version diverge da versão aprovada.")
    if politica.get("release_tag") != "fila-instagram-facebook":
        raise PoliticaErro("release_tag diverge da tag aprovada.")
    if politica.get("github_visibilidade") != "publico":
        raise PoliticaErro(
            "github_visibilidade precisa ser 'publico' para fornecer URLs diretas à Meta."
        )
    if politica.get("exposicao_midias_futuras_confirmada") is not True:
        raise PoliticaErro(
            "exposicao_midias_futuras_confirmada=false; ativação bloqueada."
        )
    reels = politica.get("reels", {})
    if not isinstance(reels, Mapping):
        raise PoliticaErro("Seção reels inválida na política.")
    # Regra do Cristiano em 10/09/2026: Reels de ate 3 minutos em todos os projetos.
    if reels.get("duracao_minima_segundos") != 3.0 or reels.get("duracao_maxima_segundos") != 180.0:
        raise PoliticaErro("Limites de duração de Reels divergem de 3–180 s.")
    if reels.get("horarios_por_semana") != HORARIOS_REELS_APROVADOS:
        raise PoliticaErro("horarios_por_semana divergem da rampa 1/2/3/4/5 aprovada.")
    if reels.get("maximo_diario_por_semana") != LIMITES_REELS_APROVADOS:
        raise PoliticaErro("maximo_diario_por_semana diverge dos tetos 1/2/3/4/5 aprovados.")
    stories = politica.get("stories", {})
    if not isinstance(stories, Mapping) or stories != {
        "horario": "09:00",
        "maximo_pacotes_fonte_iniciados_por_dia": 1,
        "limite_parte_segundos": 59.0,
        "partes_iguais": True,
    }:
        raise PoliticaErro(
            "Política de Stories diverge de 09:00, uma fonte/dia, partes iguais ou limite de 59 s."
        )
    if not politica.get("data_inicio_rampa"):
        raise PoliticaErro(
            "data_inicio_rampa está null; a ativação permanece bloqueada até definir AAAA-MM-DD."
        )
    try:
        date.fromisoformat(str(politica["data_inicio_rampa"]))
    except ValueError:
        raise PoliticaErro("data_inicio_rampa deve usar AAAA-MM-DD.") from None
    return politica


def validar_coerencia_ambiente(
    politica: dict,
    ambiente: Mapping[str, str] | None = None,
) -> None:
    """Impede que um checkout PQD publique em repositório/configuração diferente."""
    ambiente = os.environ if ambiente is None else ambiente
    repositorio_esperado = str(politica.get("repositorio_esperado") or "").strip()
    if not repositorio_esperado:
        raise PoliticaErro(
            "repositorio_esperado está null; a ativação permanece bloqueada."
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repositorio_esperado):
        raise PoliticaErro("repositorio_esperado deve usar o formato dono/repositorio.")
    comparacoes = {
        "GITHUB_REPOSITORY": repositorio_esperado,
        "PQD_META_GRAPH_VERSION": str(politica["meta_graph_version"]),
        "PQD_RELEASE_TAG": str(politica["release_tag"]),
    }
    for nome, esperado in comparacoes.items():
        atual = ambiente.get(nome, "").strip()
        if atual != esperado:
            raise PoliticaErro(f"{nome} diverge da política versionada.")
    if ambiente.get("PQD_GITHUB_VISIBILIDADE_ATUAL", "").strip().casefold() != "public":
        raise PoliticaErro("O repositório atual não foi confirmado como público pelo GitHub Actions.")


def _semana(data_item: date, inicio: date) -> int:
    diferenca = (data_item - inicio).days
    if diferenca < 0:
        raise PoliticaErro("Existe publicação anterior à data de início da rampa.")
    return diferenca // 7 + 1


def _chave_semana(numero: int) -> str:
    return str(numero) if numero <= 4 else "5+"


def chave_semana_atual(politica: dict, agora: datetime) -> str:
    inicio = date.fromisoformat(str(politica["data_inicio_rampa"]))
    return _chave_semana(_semana(agora.astimezone(BRT).date(), inicio))


def _data_hora_brt(valor: object, contexto: str) -> datetime:
    try:
        momento = datetime.fromisoformat(str(valor))
    except ValueError:
        raise PoliticaErro(f"{contexto} possui timestamp ausente ou inválido.") from None
    if momento.tzinfo is None:
        raise PoliticaErro(f"{contexto} possui timestamp sem fuso horário.")
    return momento.astimezone(BRT)


def limitar_reels_pelo_teto_real(
    selecionados: list[MutableMapping[str, object]],
    fila: dict,
    politica: dict,
    agora: datetime,
) -> tuple[list[MutableMapping[str, object]], dict[str, int]]:
    """Reserva, em ordem, no máximo o saldo diário real de cada rede."""
    hoje = agora.astimezone(BRT).date()
    chave = chave_semana_atual(politica, agora)
    limite = int(politica["reels"]["maximo_diario_por_semana"][chave])
    usados = {"instagram": 0, "facebook": 0}
    for item in fila.get("conteudos", []):
        if not isinstance(item, Mapping):
            continue
        for plataforma in usados:
            registro = item.get(plataforma, {})
            if not isinstance(registro, Mapping) or registro.get("status") != "publicado":
                continue
            publicado_em = _data_hora_brt(
                registro.get("publicado_em"),
                f"Reel {item.get('id', 'sem-id')} em {plataforma}",
            )
            if publicado_em.date() == hoje:
                usados[plataforma] += 1
    saldos = {plataforma: max(0, limite - total) for plataforma, total in usados.items()}
    reservados = dict(saldos)
    permitidos: list[MutableMapping[str, object]] = []
    for item in selecionados:
        necessarias: list[str] = []
        for plataforma in ("instagram", "facebook"):
            registro = item.get(plataforma, {})
            if not isinstance(registro, Mapping) or not (
                registro.get("status") == "publicado" and registro.get("id")
            ):
                necessarias.append(plataforma)
        if any(reservados[plataforma] <= 0 for plataforma in necessarias):
            # A ordem global é preservada: backlog posterior não atravessa o teto.
            break
        permitidos.append(item)
        for plataforma in necessarias:
            reservados[plataforma] -= 1
    return permitidos, {"limite": limite, **{f"usados_{k}": v for k, v in usados.items()}}


def story_pode_iniciar_no_dia_real(
    pacote_selecionado: Mapping[str, object],
    fila: dict,
    politica: dict,
    agora: datetime,
) -> bool:
    """Permite retomar o mesmo pacote, mas nunca iniciar um segundo no dia BRT."""
    hoje = agora.astimezone(BRT).date()

    def inicio_pacote(pacote: Mapping[str, object]) -> datetime | None:
        bruto = pacote.get("publicacao_iniciada_em")
        if bruto:
            return _data_hora_brt(bruto, f"Pacote {pacote.get('id', 'sem-id')}")
        momentos: list[datetime] = []
        partes = pacote.get("partes", [])
        if isinstance(partes, list):
            for parte in partes:
                if not isinstance(parte, Mapping):
                    continue
                for plataforma in ("instagram", "facebook"):
                    registro = parte.get(plataforma, {})
                    if not isinstance(registro, Mapping):
                        continue
                    for campo in ("publicacao_iniciada_em", "publicado_em"):
                        if registro.get(campo):
                            momentos.append(
                                _data_hora_brt(
                                    registro[campo],
                                    f"Pacote {pacote.get('id', 'sem-id')} em {plataforma}",
                                )
                            )
        return min(momentos) if momentos else None

    inicio_selecionado = inicio_pacote(pacote_selecionado)
    if inicio_selecionado is not None:
        return True
    iniciados_hoje = 0
    for pacote in fila.get("pacotes", []):
        if isinstance(pacote, Mapping):
            inicio = inicio_pacote(pacote)
            if inicio is not None and inicio.date() == hoje:
                iniciados_hoje += 1
    limite = int(politica["stories"]["maximo_pacotes_fonte_iniciados_por_dia"])
    return iniciados_hoje < limite


def _data_item(item: Mapping[str, object]) -> date:
    try:
        return date.fromisoformat(str(item["data"]))
    except (KeyError, ValueError):
        raise PoliticaErro("Item de fila com data ausente ou inválida.") from None


def _e_historico(item: Mapping[str, object], data_item: date) -> bool:
    """Diz se o item já saiu e por isso não responde mais pela rampa.

    A rampa governa o que ainda vai ao ar. Um item concluído em dia passado é
    fato consumado: reprová-lo aqui travaria a fila inteira para sempre por
    causa de algo que já aconteceu, como uma publicação manual de teste fora
    do horário da semana.
    """
    if str(item.get("status", "")) != "concluido":
        return False
    return data_item < datetime.now(BRT).date()


def _validar_cabecalho_fila(
    fila: dict,
    politica: dict,
    canal: str,
    chave_itens: str,
) -> list:
    if type(fila.get("schema_version")) is not int or fila["schema_version"] != 1:
        raise PoliticaErro("Fila com schema_version ausente ou incompatível.")
    if fila.get("canal") != canal:
        raise PoliticaErro(f"Fila com canal incompatível; esperado {canal}.")
    if fila.get("timezone") != politica.get("timezone"):
        raise PoliticaErro("Fila com timezone ausente ou divergente da política.")
    itens = fila.get(chave_itens)
    if not isinstance(itens, list):
        raise PoliticaErro(f"Fila precisa conter uma lista em {chave_itens}.")
    if chave_itens == "pacotes":
        limite = fila.get("limite_parte_segundos")
        limite_esperado = politica.get("stories", {}).get("limite_parte_segundos")
        if (
            isinstance(limite, bool)
            or not isinstance(limite, (int, float))
            or float(limite) != float(limite_esperado)
        ):
            raise PoliticaErro("Fila de Stories com limite_parte_segundos incompatível.")
    return itens


def _validar_midia_release(
    midia: object,
    politica: dict,
    contexto: str,
) -> Mapping[str, object]:
    if not isinstance(midia, Mapping):
        raise PoliticaErro(f"{contexto} não possui metadados de mídia.")
    asset = str(midia.get("asset", "")).strip()
    if not asset or asset in {".", ".."} or "/" in asset or "\\" in asset:
        raise PoliticaErro(f"{contexto} possui nome de asset ausente ou inseguro.")
    digest = str(midia.get("sha256", "")).strip().casefold()
    tamanho = midia.get("tamanho_bytes")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PoliticaErro(f"{contexto} possui SHA-256 ausente ou inválido.")
    if isinstance(tamanho, bool) or not isinstance(tamanho, int) or tamanho <= 0:
        raise PoliticaErro(f"{contexto} possui tamanho_bytes ausente ou inválido.")
    repositorio = str(politica.get("repositorio_esperado", ""))
    tag = str(politica.get("release_tag", ""))
    url_esperada = (
        f"https://github.com/{repositorio}/releases/download/"
        f"{quote(tag, safe='')}/{quote(asset, safe='')}"
    )
    if midia.get("url_publica") != url_esperada:
        raise PoliticaErro(
            f"{contexto} não aponta para a Release e o repositório autorizados."
        )
    return midia


def validar_fila_reels(fila: dict, politica: dict) -> None:
    itens = _validar_cabecalho_fila(
        fila, politica, "instagram-facebook-reels", "conteudos"
    )
    inicio = date.fromisoformat(str(politica["data_inicio_rampa"]))
    reels = politica.get("reels", {})
    if not isinstance(reels, Mapping):
        raise PoliticaErro("Seção reels inválida na política.")
    horarios_semana = reels.get("horarios_por_semana", {})
    limites_semana = reels.get("maximo_diario_por_semana", {})
    if not isinstance(horarios_semana, Mapping) or not isinstance(limites_semana, Mapping):
        raise PoliticaErro("Rampa de Reels incompleta na política.")

    slots: Counter[tuple[str, str]] = Counter()
    por_dia: Counter[str] = Counter()
    limite_por_dia: dict[str, int] = {}
    for item in itens:
        if not isinstance(item, MutableMapping):
            raise PoliticaErro("A fila de Reels contém item inválido.")
        data_item = _data_item(item)
        semana = _semana(data_item, inicio)
        chave = _chave_semana(semana)
        try:
            permitidos = tuple(str(valor) for valor in horarios_semana[chave])
            limite = int(limites_semana[chave])
        except (KeyError, TypeError, ValueError):
            raise PoliticaErro(f"Política de Reels incompleta para a semana {semana}.") from None
        historico = _e_historico(item, data_item)
        horario = str(item.get("horario", ""))
        if horario not in permitidos and not historico:
            raise PoliticaErro(
                f"Reel {item.get('id', 'sem-id')} usa {horario or 'horário ausente'}, "
                f"fora da rampa da semana {semana}: {', '.join(permitidos)}."
            )
        identificador = f"Reel {item.get('id', 'sem-id')}"
        midia = _validar_midia_release(item.get("midia"), politica, identificador)
        try:
            duracao = float(midia.get("duracao_segundos", 0))
        except (TypeError, ValueError):
            raise PoliticaErro("Duração de Reel inválida na fila.") from None
        if not math.isfinite(duracao) or duracao < 3.0 or duracao > 180.0:
            raise PoliticaErro(
                f"Reel {item.get('id', 'sem-id')} fora do intervalo de duração 3–180 s."
            )
        for plataforma in ("instagram", "facebook"):
            registro = item.get(plataforma)
            if not isinstance(registro, Mapping):
                raise PoliticaErro(f"{identificador} não possui estado de {plataforma}.")
            if registro.get("legenda") != politica.get("legenda_reels"):
                raise PoliticaErro(
                    f"{identificador} possui legenda de {plataforma} divergente da política."
                )
        if not historico:
            data_texto = data_item.isoformat()
            slots[(data_texto, horario)] += 1
            por_dia[data_texto] += 1
            limite_por_dia[data_texto] = limite

    duplicados = [f"{data} {hora}" for (data, hora), total in slots.items() if total > 1]
    if duplicados:
        raise PoliticaErro(f"Slots duplicados de Reels: {', '.join(sorted(duplicados))}.")
    excessos = [
        f"{data}: {total}>{limite_por_dia[data]}"
        for data, total in por_dia.items()
        if total > limite_por_dia[data]
    ]
    if excessos:
        raise PoliticaErro(f"Teto diário real da rampa excedido: {', '.join(sorted(excessos))}.")


def _pacote_iniciado(pacote: Mapping[str, object]) -> bool:
    partes = pacote.get("partes", [])
    if not isinstance(partes, list):
        raise PoliticaErro("Pacote de Stories com lista de partes inválida.")
    for parte in partes:
        if not isinstance(parte, Mapping):
            continue
        for plataforma in ("instagram", "facebook"):
            registro = parte.get(plataforma, {})
            if isinstance(registro, Mapping) and str(registro.get("status", "pendente")) not in {
                "",
                "pendente",
            }:
                return True
    return False


def validar_fila_stories(fila: dict, politica: dict) -> None:
    pacotes = _validar_cabecalho_fila(
        fila, politica, "instagram-facebook-stories", "pacotes"
    )
    inicio = date.fromisoformat(str(politica["data_inicio_rampa"]))
    stories = politica.get("stories", {})
    if not isinstance(stories, Mapping):
        raise PoliticaErro("Seção stories inválida na política.")
    horario_oficial = str(stories.get("horario", ""))
    try:
        maximo_iniciados = int(stories.get("maximo_pacotes_fonte_iniciados_por_dia", 0))
    except (TypeError, ValueError):
        raise PoliticaErro("Teto de pacotes de Stories inválido.") from None
    if horario_oficial != "09:00" or maximo_iniciados != 1:
        raise PoliticaErro("A política de Stories deve fixar 09:00 e um pacote-fonte iniciado/dia.")

    pacotes_por_dia: Counter[str] = Counter()
    iniciados_por_dia: Counter[str] = Counter()
    for pacote in pacotes:
        if not isinstance(pacote, MutableMapping):
            raise PoliticaErro("A fila de Stories contém pacote inválido.")
        data_pacote = _data_item(pacote)
        _semana(data_pacote, inicio)
        horario = str(pacote.get("horario", horario_oficial))
        if horario != horario_oficial and not _e_historico(pacote, data_pacote):
            raise PoliticaErro(
                f"Pacote {pacote.get('id', 'sem-id')} está em {horario}; Stories somente às 09:00."
            )
        data_texto = data_pacote.isoformat()
        pacotes_por_dia[data_texto] += 1
        if _pacote_iniciado(pacote):
            iniciados_por_dia[data_texto] += 1
        partes = pacote.get("partes", [])
        if not isinstance(partes, list) or not partes:
            raise PoliticaErro(f"Pacote {pacote.get('id', 'sem-id')} não possui partes.")
        ordens = [parte.get("ordem") if isinstance(parte, Mapping) else None for parte in partes]
        if any(isinstance(ordem, bool) or not isinstance(ordem, int) for ordem in ordens):
            raise PoliticaErro("As partes do Story precisam ter ordens inteiras.")
        if sorted(ordens) != list(range(1, len(partes) + 1)):
            raise PoliticaErro("As partes do Story precisam ter ordens contíguas a partir de 1.")
        duracoes: list[float] = []
        for parte in partes:
            if not isinstance(parte, Mapping):
                raise PoliticaErro(f"Pacote {pacote.get('id', 'sem-id')} possui parte inválida.")
            midia = _validar_midia_release(
                parte.get("midia"),
                politica,
                f"Pacote {pacote.get('id', 'sem-id')} parte {parte.get('ordem', '?')}",
            )
            try:
                duracao = float(midia.get("duracao_segundos", 0))
            except (TypeError, ValueError):
                raise PoliticaErro("Duração de Story inválida na fila.") from None
            if not math.isfinite(duracao) or duracao <= 0 or duracao > 59.0:
                raise PoliticaErro("Parte de Story fora do limite estrito de 59 s.")
            duracoes.append(duracao)
        if max(duracoes) - min(duracoes) > TOLERANCIA_IGUALDADE_STORY_SEGUNDOS:
            raise PoliticaErro(
                f"Pacote {pacote.get('id', 'sem-id')} não possui partes iguais "
                f"(tolerância {TOLERANCIA_IGUALDADE_STORY_SEGUNDOS:g} s)."
            )

    duplicados = [data for data, total in pacotes_por_dia.items() if total > 1]
    if duplicados:
        raise PoliticaErro(
            "Mais de um pacote-fonte de Stories agendado no mesmo dia: " + ", ".join(sorted(duplicados)) + "."
        )
    excessos = [data for data, total in iniciados_por_dia.items() if total > maximo_iniciados]
    if excessos:
        raise PoliticaErro(
            "Mais de um pacote-fonte de Stories iniciado no mesmo dia: " + ", ".join(sorted(excessos)) + "."
        )


def validar_filas(fila_reels: dict, fila_stories: dict, politica: dict | None = None) -> dict:
    politica = politica or carregar_politica()
    validar_fila_reels(fila_reels, politica)
    validar_fila_stories(fila_stories, politica)
    return politica


def validar_manual_nao_futuro(momento: datetime, agora: datetime | None = None) -> None:
    agora = agora or datetime.now(BRT)
    if momento > agora:
        raise PoliticaErro("A execução manual não pode publicar item futuro.")
