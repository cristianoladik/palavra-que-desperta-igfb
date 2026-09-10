"""Coordenador único: preflight, política, publicação e limpeza compartilhada."""

from __future__ import annotations

from automacao_comum import (
    MetaClient,
    automacao_ativa,
    carregar_json,
    emitir_resumo,
    exigir_persistencia_duravel,
    mensagem_segura,
    mensagem_travas,
    validar_preflight_meta,
)
from limpar_release import FILA_REELS, FILA_STORIES, executar as limpar
from politica_agenda import carregar_politica, validar_coerencia_ambiente, validar_filas
from publicar import executar as publicar_reels
from publicar_stories import executar as publicar_stories


def executar(cliente: MetaClient | None = None) -> int:
    if not automacao_ativa():
        emitir_resumo("DESATIVADO", "Coordenador Meta PQD", (mensagem_travas(),))
        return 0
    exigir_persistencia_duravel()
    fila_reels = carregar_json(FILA_REELS)
    fila_stories = carregar_json(FILA_STORIES)
    politica = carregar_politica()
    validar_filas(fila_reels, fila_stories, politica)
    validar_coerencia_ambiente(politica)

    cliente = cliente or MetaClient()
    # O primeiro acesso externo só ocorre depois da política: dois GETs Meta.
    validar_preflight_meta(cliente)

    # Stories têm prioridade às 09h; Reels continuam independentes mesmo se
    # o pacote de Stories falhar. Qualquer erro apenas bloqueia a limpeza.
    codigo_stories = publicar_stories(
        cliente=cliente,
        preflight_realizado=True,
        politica_validada=True,
        requisitos_ativacao_validados=True,
        politica_execucao=politica,
    )
    codigo_reels = publicar_reels(
        cliente=cliente,
        preflight_realizado=True,
        politica_validada=True,
        requisitos_ativacao_validados=True,
        politica_execucao=politica,
    )
    if codigo_reels or codigo_stories:
        emitir_resumo(
            "ERRO",
            "Coordenador Meta PQD",
            (
                f"Publicação encerrou com Reels={codigo_reels}, Stories={codigo_stories}.",
                "A limpeza da release foi bloqueada nesta execução.",
            ),
        )
        return 1

    codigo_limpeza = limpar(
        (FILA_REELS, FILA_STORIES),
        cliente_meta=cliente,
        preflight_realizado=True,
        politica_validada=True,
        requisitos_ativacao_validados=True,
    )
    emitir_resumo(
        "SUCESSO" if codigo_limpeza == 0 else "ERRO",
        "Coordenador Meta PQD",
        ("Ciclo único de Reels, Stories e limpeza compartilhada concluído.",),
    )
    return codigo_limpeza


def main() -> None:
    try:
        codigo = executar()
    except Exception as erro:
        emitir_resumo("ERRO", "Coordenador Meta PQD", (mensagem_segura(erro),))
        raise
    raise SystemExit(codigo)


if __name__ == "__main__":
    main()
