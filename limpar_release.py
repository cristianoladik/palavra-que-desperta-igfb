"""Limpa a release usando Reels e Stories como um único universo de referências."""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping, Sequence
from urllib.parse import quote

import requests

from automacao_comum import (
    BRT,
    MetaClient,
    ROOT,
    automacao_ativa,
    carregar_json,
    criar_persistidor,
    emitir_resumo,
    exigir_persistencia_duravel,
    item_concluido,
    mensagem_segura,
    mensagem_travas,
    obrigatoria,
    redigir_segredos,
    validar_preflight_meta,
)
from politica_agenda import carregar_politica, validar_coerencia_ambiente, validar_filas


FILA_REELS = ROOT / "fila" / "fila-reels.json"
FILA_STORIES = ROOT / "fila" / "fila-stories.json"


class GitHubReleaseClient:
    def __init__(self, repositorio: str, token: str, sessao=None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repositorio):
            raise RuntimeError("GITHUB_REPOSITORY inválido.")
        self.repositorio = repositorio
        self.token = token
        self.sessao = sessao or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, url: str, **kwargs):
        try:
            return self.sessao.get(url, headers=self.headers, timeout=30, **kwargs)
        except Exception as erro:
            raise RuntimeError(
                f"Falha segura ao consultar a release: {mensagem_segura(erro, (self.token,))}"
            ) from None

    def assets(self, tag: str) -> list[dict]:
        resposta = self._get(
            f"https://api.github.com/repos/{self.repositorio}/releases/tags/{quote(tag, safe='')}"
        )
        if not resposta.ok:
            raise RuntimeError(f"GitHub HTTP {resposta.status_code} ao consultar a release.")
        try:
            release_id = int(resposta.json()["id"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("A resposta da release não contém ID válido.") from None
        assets: list[dict] = []
        pagina = 1
        while True:
            resposta_assets = self._get(
                f"https://api.github.com/repos/{self.repositorio}/releases/{release_id}/assets",
                params={"per_page": 100, "page": pagina},
            )
            if not resposta_assets.ok:
                raise RuntimeError(
                    f"GitHub HTTP {resposta_assets.status_code} ao listar assets da release."
                )
            lote = resposta_assets.json()
            if not isinstance(lote, list):
                raise RuntimeError("A resposta da release não contém uma lista de assets.")
            assets.extend(lote)
            if len(lote) < 100:
                break
            pagina += 1
        return assets

    def excluir(self, asset_id: int) -> None:
        try:
            resposta = self.sessao.delete(
                f"https://api.github.com/repos/{self.repositorio}/releases/assets/{asset_id}",
                headers=self.headers,
                timeout=30,
            )
        except Exception as erro:
            raise RuntimeError(
                f"Falha segura ao excluir asset da release: {mensagem_segura(erro, (self.token,))}"
            ) from None
        if resposta.status_code != 204:
            raise RuntimeError(f"GitHub HTTP {resposta.status_code} ao excluir o asset.")


def iterar_midias(fila: dict) -> Iterable[tuple[MutableMapping[str, object], bool]]:
    if "conteudos" in fila:
        for item in fila.get("conteudos", []):
            if isinstance(item, MutableMapping) and isinstance(item.get("midia"), MutableMapping):
                yield item["midia"], item_concluido(item)  # type: ignore[misc]
        return
    for pacote in fila.get("pacotes", []):
        if not isinstance(pacote, MutableMapping):
            continue
        for parte in pacote.get("partes", []):
            if isinstance(parte, MutableMapping) and isinstance(parte.get("midia"), MutableMapping):
                yield parte["midia"], item_concluido(parte)  # type: ignore[misc]


def chave_midia(midia: Mapping[str, object]) -> tuple[str, int]:
    digest = str(midia.get("sha256", "")).casefold()
    try:
        tamanho = int(midia.get("tamanho_bytes", 0))
    except (TypeError, ValueError):
        raise RuntimeError("tamanho_bytes inválido na fila.") from None
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or tamanho <= 0:
        raise RuntimeError("SHA-256 ou tamanho ausente na fila.")
    return digest, tamanho


def _normalizar_caminhos(filas_paths: Sequence[Path] | None) -> tuple[Path, Path]:
    if filas_paths is None:
        return FILA_REELS, FILA_STORIES
    caminhos = tuple(Path(item) for item in filas_paths)
    if len(caminhos) != 2:
        raise RuntimeError("A limpeza exige, juntas, as filas de Reels e Stories.")
    return caminhos[0], caminhos[1]


def executar(
    filas_paths: Sequence[Path] | None = None,
    cliente: GitHubReleaseClient | None = None,
    repositorio: str | None = None,
    tag: str | None = None,
    cliente_meta: MetaClient | None = None,
    preflight_realizado: bool = False,
    politica_validada: bool = False,
    requisitos_ativacao_validados: bool = False,
) -> int:
    titulo = "Limpeza compartilhada da release — Reels e Stories"
    if not automacao_ativa():
        emitir_resumo("DESATIVADO", titulo, (mensagem_travas(),))
        return 0

    if not requisitos_ativacao_validados:
        exigir_persistencia_duravel()
    fila_reels_path, fila_stories_path = _normalizar_caminhos(filas_paths)
    filas = {
        fila_reels_path: carregar_json(fila_reels_path),
        fila_stories_path: carregar_json(fila_stories_path),
    }
    if not politica_validada:
        politica = carregar_politica()
        validar_filas(filas[fila_reels_path], filas[fila_stories_path], politica)
        validar_coerencia_ambiente(politica)
    cliente_meta = cliente_meta or MetaClient()
    if not preflight_realizado:
        validar_preflight_meta(cliente_meta)

    # O universo abaixo é obrigatoriamente compartilhado. Uma referência ativa
    # em qualquer fila bloqueia a remoção do asset para ambas.
    ativos: set[tuple[str, int]] = set()
    ativos_nomes: set[str] = set()
    metadados_por_nome: dict[str, set[tuple[str, int]]] = defaultdict(set)
    nomes_invalidos: set[str] = set()
    candidatos: dict[tuple[str, str, int], list[MutableMapping[str, object]]] = defaultdict(list)
    erros: list[str] = []
    for fila in filas.values():
        for midia, publicado in iterar_midias(fila):
            asset = str(midia.get("asset", "")).strip()
            nome_seguro = bool(asset and Path(asset).name == asset)
            if not publicado and nome_seguro:
                ativos_nomes.add(asset)
            try:
                digest, tamanho = chave_midia(midia)
            except Exception as erro:
                erros.append(mensagem_segura(erro))
                if nome_seguro:
                    nomes_invalidos.add(asset)
                continue
            if nome_seguro:
                metadados_por_nome[asset].add((digest, tamanho))
            if not publicado:
                ativos.add((digest, tamanho))
                continue
            if midia.get("removido_da_release_em"):
                continue
            if not nome_seguro:
                erros.append("Nome de asset ausente ou inseguro na fila.")
                continue
            candidatos[(asset, digest, tamanho)].append(midia)

    if not candidatos and not erros:
        emitir_resumo("SEM_ASSETS_ELEGIVEIS", titulo, ("0 assets excluídos.",))
        return 0

    repositorio = repositorio or obrigatoria("GITHUB_REPOSITORY")
    tag = tag or os.getenv("PQD_RELEASE_TAG", "fila-instagram-facebook").strip()
    if not tag:
        raise RuntimeError("PQD_RELEASE_TAG não pode ser vazia.")
    token = obrigatoria("PQD_GITHUB_CONTENTS_TOKEN")
    cliente = cliente or GitHubReleaseClient(repositorio, token)
    assets = {str(asset.get("name", "")): asset for asset in cliente.assets(tag)}
    persistidores = {
        caminho: criar_persistidor(caminho, fila) for caminho, fila in filas.items()
    }

    excluidos = bloqueados = 0
    for (nome, digest, tamanho), registros in candidatos.items():
        if nome in ativos_nomes or (digest, tamanho) in ativos:
            erros.append(f"Asset {nome} ainda é referenciado por item não concluído.")
            bloqueados += 1
            continue
        if nome in nomes_invalidos or len(metadados_por_nome.get(nome, set())) != 1:
            erros.append(f"Asset {nome} possui referências com metadados inconsistentes.")
            bloqueados += 1
            continue
        asset = assets.get(nome)
        if not asset:
            erros.append(f"Asset {nome} não foi encontrado; exclusão não comprovada.")
            bloqueados += 1
            continue
        digest_remoto = str(asset.get("digest", "")).removeprefix("sha256:").casefold()
        try:
            tamanho_remoto = int(asset.get("size", -1))
            asset_id = int(asset["id"])
        except (KeyError, TypeError, ValueError):
            erros.append(f"Metadados remotos incompletos para {nome}.")
            bloqueados += 1
            continue
        if digest_remoto != digest or tamanho_remoto != tamanho:
            erros.append(f"SHA-256/tamanho divergente para {nome}; nada foi excluído.")
            bloqueados += 1
            continue

        iniciado_em = datetime.now(BRT).isoformat()
        for registro in registros:
            registro["remocao_release_iniciada_em"] = iniciado_em
        for persistir in persistidores.values():
            persistir(f"release:antes_de_excluir:{nome}")

        cliente.excluir(asset_id)
        removido_em = datetime.now(BRT).isoformat()
        for registro in registros:
            registro["removido_da_release_em"] = removido_em
            registro.pop("remocao_release_iniciada_em", None)
        # Ambas as filas são persistidas a cada exclusão, inclusive quando só
        # uma delas continha o asset.
        for persistir in persistidores.values():
            persistir(f"release:depois_de_excluir:{nome}")
        excluidos += 1

    resultado = "ERRO" if erros else "SUCESSO"
    linhas = [f"{excluidos} assets excluídos após validação compartilhada por SHA-256 e tamanho."]
    if bloqueados:
        linhas.append(f"{bloqueados} assets bloqueados sem exclusão.")
    linhas.extend(redigir_segredos(erro) for erro in erros[:10])
    emitir_resumo(resultado, titulo, tuple(linhas))
    return 1 if erros else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fila-reels", type=Path, default=FILA_REELS)
    parser.add_argument("--fila-stories", type=Path, default=FILA_STORIES)
    args = parser.parse_args()
    try:
        codigo = executar((args.fila_reels, args.fila_stories))
    except Exception as erro:
        emitir_resumo("ERRO", "Limpeza compartilhada da release", (mensagem_segura(erro),))
        raise
    raise SystemExit(codigo)


if __name__ == "__main__":
    main()
