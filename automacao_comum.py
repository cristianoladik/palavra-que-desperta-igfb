"""Primitivas de segurança da automação Meta do Palavra Que Desperta.

As travas e credenciais são deliberadamente namespaced. Toda publicação
começa por um preflight somente-leitura das contas e cada checkpoint pode ser
gravado, de forma otimista, pela GitHub Contents API.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, MutableMapping
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parent
BRT = ZoneInfo("America/Sao_Paulo")
PLATAFORMAS = ("instagram", "facebook")
VALORES_ATIVOS = frozenset({"1", "true", "sim", "yes", "on"})
GRAPH_VERSION_PADRAO = "v26.0"
USUARIO_INSTAGRAM_ESPERADO = "palavraquedesperta_br"
RAIZ_DRIVE_OFICIAL = Path(
    r"G:\Meu Drive\PROJETO SEGUNDO CÉREBRO Cristiano Ladik\Projeto - Palavra Que Desperta - Drive"
)

TRAVA_AUTOMACAO = "PQD_AUTOMACAO_ATIVA"
TRAVA_CONFIRMACAO = "PQD_PUBLICACAO_CONFIRMADA"
NOMES_CREDENCIAIS_META = (
    "PQD_IG_ACCESS_TOKEN",
    "PQD_IG_BUSINESS_ID",
    "PQD_FB_PAGE_ACCESS_TOKEN",
    "PQD_FB_PAGE_ID",
)
NOMES_SEGREDOS = (
    "PQD_IG_ACCESS_TOKEN",
    "PQD_FB_PAGE_ACCESS_TOKEN",
    "PQD_GITHUB_CONTENTS_TOKEN",
)


def agora_brt() -> datetime:
    return datetime.now(BRT)


def valor_ativo(nome: str, ambiente: Mapping[str, str] | None = None) -> bool:
    ambiente = os.environ if ambiente is None else ambiente
    return ambiente.get(nome, "false").strip().casefold() in VALORES_ATIVOS


def automacao_ativa(ambiente: Mapping[str, str] | None = None) -> bool:
    """Exige as duas confirmações independentes do projeto."""
    return valor_ativo(TRAVA_AUTOMACAO, ambiente) and valor_ativo(TRAVA_CONFIRMACAO, ambiente)


def mensagem_travas() -> str:
    return (
        f"{TRAVA_AUTOMACAO}=true e {TRAVA_CONFIRMACAO}=true são obrigatórias; "
        "nenhuma chamada ou alteração foi feita."
    )


def exigir_persistencia_duravel(ambiente: Mapping[str, str] | None = None) -> None:
    if not valor_ativo("PQD_PERSISTENCIA_GITHUB", ambiente):
        raise RuntimeError(
            "PQD_PERSISTENCIA_GITHUB=true é obrigatória para ativação real."
        )


def obrigatoria(nome: str, ambiente: Mapping[str, str] | None = None) -> str:
    ambiente = os.environ if ambiente is None else ambiente
    valor = ambiente.get(nome, "").strip()
    if not valor:
        raise RuntimeError(f"Configuração obrigatória ausente: {nome}")
    return valor


def _segredos_ambiente(ambiente: Mapping[str, str] | None = None) -> tuple[str, ...]:
    ambiente = os.environ if ambiente is None else ambiente
    return tuple(ambiente.get(nome, "").strip() for nome in NOMES_SEGREDOS if ambiente.get(nome, "").strip())


def redigir_segredos(valor: object, extras: tuple[str, ...] = ()) -> str:
    texto = str(valor)
    segredos = (*_segredos_ambiente(), *extras)
    for segredo in sorted({item for item in segredos if item}, key=len, reverse=True):
        texto = texto.replace(segredo, "<redigido>")
        texto = texto.replace(quote(segredo, safe=""), "<redigido>")
    return texto


def mensagem_segura(erro: BaseException, extras: tuple[str, ...] = ()) -> str:
    texto = redigir_segredos(erro, extras).strip()
    return texto or erro.__class__.__name__


def _sanitizar_para_persistencia(valor):
    if isinstance(valor, str):
        return redigir_segredos(valor)
    if isinstance(valor, dict):
        return {chave: _sanitizar_para_persistencia(item) for chave, item in valor.items()}
    if isinstance(valor, list):
        return [_sanitizar_para_persistencia(item) for item in valor]
    return valor


def emitir_resumo(resultado: str, titulo: str, linhas: list[str] | tuple[str, ...] = ()) -> None:
    """Escreve um resultado inequívoco sem ecoar Secrets conhecidos."""
    titulo = redigir_segredos(titulo)
    linhas_seguras = tuple(redigir_segredos(linha) for linha in linhas)
    bloco = [f"RESULTADO: {redigir_segredos(resultado)}", *linhas_seguras]
    print(f"[{titulo}] " + " | ".join(bloco))
    caminho = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not caminho:
        return
    with Path(caminho).open("a", encoding="utf-8", newline="\n") as arquivo:
        arquivo.write(f"## {titulo}\n\n")
        arquivo.write(f"**RESULTADO: {redigir_segredos(resultado)}**\n\n")
        for linha in linhas_seguras:
            arquivo.write(f"- {linha}\n")
        arquivo.write("\n")


def carregar_json(caminho: Path) -> dict:
    return json.loads(caminho.read_text(encoding="utf-8"))


def salvar_json_atomico(caminho: Path, dados: dict) -> None:
    """Grava JSON sanitizado e troca o arquivo somente após flush/fsync."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(f".{caminho.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    seguros = _sanitizar_para_persistencia(copy.deepcopy(dados))
    try:
        with temporario.open("x", encoding="utf-8", newline="\n") as arquivo:
            json.dump(seguros, arquivo, ensure_ascii=False, indent=2)
            arquivo.write("\n")
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, caminho)
    finally:
        temporario.unlink(missing_ok=True)


def sha256_arquivo(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def plataforma_publicada(registro: Mapping[str, object]) -> bool:
    return registro.get("status") == "publicado" and bool(registro.get("id"))


def item_concluido(item: Mapping[str, object]) -> bool:
    return all(
        plataforma_publicada(item.get(plataforma, {}))  # type: ignore[arg-type]
        for plataforma in PLATAFORMAS
    )


def momento_item(item: Mapping[str, object], horario_padrao: str) -> datetime:
    data = str(item["data"])
    horario = str(item.get("horario", horario_padrao))
    return datetime.fromisoformat(f"{data}T{horario}:00").replace(tzinfo=BRT)


class MetaErro(RuntimeError):
    """Erro seguro da Graph API, sem ecoar tokens conhecidos."""


class MetaMidiaTerminalErro(MetaErro):
    """O container do Instagram não pode ser retomado automaticamente."""


class MetaClient:
    def __init__(self, versao: str | None = None, sessao=None) -> None:
        versao = (versao or os.getenv("PQD_META_GRAPH_VERSION", GRAPH_VERSION_PADRAO)).strip()
        if not re.fullmatch(r"v\d+\.\d+", versao):
            raise RuntimeError(f"PQD_META_GRAPH_VERSION inválida: {versao!r}")
        self.versao = versao
        self.base = f"https://graph.facebook.com/{versao}"
        self.sessao = sessao or requests.Session()

    @staticmethod
    def _json_resposta(resposta, segredos: tuple[str, ...]) -> dict:
        if not resposta.ok:
            corpo = redigir_segredos(resposta.text[:4000], segredos)
            raise MetaErro(f"Meta HTTP {resposta.status_code}: {corpo}")
        try:
            dados = resposta.json()
        except ValueError:
            raise MetaErro("A Meta retornou uma resposta que não é JSON.") from None
        if not isinstance(dados, dict):
            raise MetaErro("A Meta retornou um JSON inesperado.")
        return dados

    def get(self, caminho: str, parametros: dict, timeout: int = 30) -> dict:
        segredos = tuple(str(v) for k, v in parametros.items() if "token" in k.casefold())
        try:
            resposta = self.sessao.get(
                f"{self.base}/{caminho.lstrip('/')}", params=parametros, timeout=timeout
            )
        except Exception as erro:
            raise MetaErro(f"Falha de transporte Meta em GET: {mensagem_segura(erro, segredos)}") from None
        return self._json_resposta(resposta, segredos)

    def post(self, caminho: str, dados: dict, timeout: int = 60) -> dict:
        segredos = tuple(str(v) for k, v in dados.items() if "token" in k.casefold())
        try:
            resposta = self.sessao.post(
                f"{self.base}/{caminho.lstrip('/')}", data=dados, timeout=timeout
            )
        except Exception as erro:
            raise MetaErro(f"Falha de transporte Meta em POST: {mensagem_segura(erro, segredos)}") from None
        return self._json_resposta(resposta, segredos)

    def aguardar_instagram(
        self,
        container_id: str,
        token: str,
        tentativas: int = 36,
        intervalo: float = 10.0,
    ) -> None:
        for tentativa in range(tentativas):
            dados = self.get(
                container_id,
                {"fields": "status_code,status", "access_token": token},
            )
            codigo = str(dados.get("status_code", ""))
            print(f"Instagram [{tentativa + 1}/{tentativas}]: {codigo or 'SEM_STATUS'}")
            if codigo == "FINISHED":
                return
            if codigo in {"ERROR", "EXPIRED"}:
                raise MetaMidiaTerminalErro(
                    f"O Instagram não processou a mídia (status {codigo})."
                )
            if tentativa + 1 < tentativas:
                time.sleep(intervalo)
        raise TimeoutError("O Instagram excedeu o tempo de processamento.")

    def upload(self, upload_url: str, token: str, caminho: Path, timeout: int = 900) -> None:
        tamanho = caminho.stat().st_size
        try:
            with caminho.open("rb") as arquivo:
                resposta = self.sessao.post(
                    upload_url,
                    headers={
                        "Authorization": f"OAuth {token}",
                        "offset": "0",
                        "file_size": str(tamanho),
                    },
                    data=arquivo,
                    timeout=timeout,
                )
        except Exception as erro:
            raise MetaErro(f"Falha de transporte no upload Meta: {mensagem_segura(erro, (token, upload_url))}") from None
        if not resposta.ok:
            corpo = redigir_segredos(resposta.text[:4000], (token, upload_url))
            raise MetaErro(f"Upload Meta HTTP {resposta.status_code}: {corpo}")


@dataclass(frozen=True)
class PreflightMeta:
    instagram_id: str
    instagram_username: str
    facebook_page_id: str


def validar_preflight_meta(
    cliente: MetaClient,
    ambiente: Mapping[str, str] | None = None,
) -> PreflightMeta:
    """Confere as quatro credenciais e o vínculo Page↔IG somente com GETs."""
    ambiente = os.environ if ambiente is None else ambiente
    ig_token = obrigatoria("PQD_IG_ACCESS_TOKEN", ambiente)
    ig_id = obrigatoria("PQD_IG_BUSINESS_ID", ambiente)
    page_token = obrigatoria("PQD_FB_PAGE_ACCESS_TOKEN", ambiente)
    page_id = obrigatoria("PQD_FB_PAGE_ID", ambiente)

    ig = cliente.get(ig_id, {"fields": "id,username", "access_token": ig_token})
    if str(ig.get("id", "")) != ig_id:
        raise RuntimeError("Preflight recusado: o ID retornado da conta Instagram diverge.")
    username = str(ig.get("username", "")).lstrip("@").casefold()
    if username != USUARIO_INSTAGRAM_ESPERADO:
        raise RuntimeError(
            "Preflight recusado: a conta Instagram não é @palavraquedesperta_br."
        )

    pagina = cliente.get(
        page_id,
        {
            "fields": "id,name,instagram_business_account{id,username}",
            "access_token": page_token,
        },
    )
    if str(pagina.get("id", "")) != page_id:
        raise RuntimeError("Preflight recusado: o ID retornado da Page diverge.")
    ligada = pagina.get("instagram_business_account")
    if not isinstance(ligada, Mapping):
        raise RuntimeError("Preflight recusado: a Page não possui conta Instagram ligada.")
    ligada_id = str(ligada.get("id", ""))
    ligada_username = str(ligada.get("username", "")).lstrip("@").casefold()
    if ligada_id != ig_id or ligada_username != USUARIO_INSTAGRAM_ESPERADO:
        raise RuntimeError(
            "Preflight recusado: a Page não está ligada à conta @palavraquedesperta_br configurada."
        )
    return PreflightMeta(ig_id, username, page_id)


class GitHubContentsClient:
    """Persistência otimista de um arquivo, sem configurar credencial no Git."""

    def __init__(self, repositorio: str, token: str, branch: str = "main", sessao=None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repositorio):
            raise RuntimeError("GITHUB_REPOSITORY inválido.")
        if not branch or any(caractere.isspace() for caractere in branch):
            raise RuntimeError("PQD_GITHUB_BRANCH inválida.")
        self.repositorio = repositorio
        self.token = token
        self.branch = branch
        self.sessao = sessao or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def atualizar(
        self,
        caminho_repo: str,
        conteudo: bytes,
        fase: str,
        conteudo_base_esperado: bytes,
    ) -> None:
        caminho_seguro = "/".join(quote(parte, safe="") for parte in Path(caminho_repo).parts)
        url = f"https://api.github.com/repos/{self.repositorio}/contents/{caminho_seguro}"
        try:
            atual = self.sessao.get(
                url,
                headers=self.headers,
                params={"ref": self.branch},
                timeout=30,
            )
        except Exception as erro:
            raise RuntimeError(
                f"Falha segura ao consultar checkpoint no GitHub: {mensagem_segura(erro, (self.token,))}"
            ) from None
        if not atual.ok:
            raise RuntimeError(
                f"GitHub HTTP {atual.status_code} ao consultar checkpoint; nada foi sobrescrito."
            )
        try:
            dados_atuais = atual.json()
            sha = str(dados_atuais["sha"])
            conteudo_remoto = base64.b64decode(
                str(dados_atuais["content"]).replace("\n", ""), validate=True
            )
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                "GitHub não retornou SHA/conteúdo válidos do checkpoint existente."
            ) from None
        if conteudo_remoto != conteudo_base_esperado:
            raise RuntimeError(
                "Conflito no checkpoint: a fila remota mudou desde o checkout; nada foi sobrescrito."
            )
        fase_segura = redigir_segredos(fase).replace("\r", " ").replace("\n", " ")[:80]
        mensagem = f"checkpoint PQD: {fase_segura} (run {os.getenv('GITHUB_RUN_ID', 'local')})"
        payload = {
            "message": mensagem,
            "content": base64.b64encode(conteudo).decode("ascii"),
            "sha": sha,
            "branch": self.branch,
        }
        try:
            resposta = self.sessao.put(url, headers=self.headers, json=payload, timeout=30)
        except Exception as erro:
            raise RuntimeError(
                f"Falha segura ao persistir checkpoint no GitHub: {mensagem_segura(erro, (self.token,))}"
            ) from None
        if not resposta.ok:
            raise RuntimeError(
                f"GitHub HTTP {resposta.status_code} ao persistir checkpoint; execução bloqueada."
            )


class PersistidorFila:
    def __init__(
        self,
        caminho: Path,
        dados: dict,
        cliente_github: GitHubContentsClient | None = None,
        caminho_repo: str | None = None,
    ) -> None:
        self.caminho = caminho
        self.dados = dados
        self.cliente_github = cliente_github
        self.caminho_repo = caminho_repo
        self._ultimo_confirmado = caminho.read_bytes() if caminho.exists() else b""

    def __call__(self, fase: str = "checkpoint") -> None:
        salvar_json_atomico(self.caminho, self.dados)
        if self.cliente_github is not None:
            if not self.caminho_repo:
                raise RuntimeError("Caminho do checkpoint no repositório não foi definido.")
            conteudo = self.caminho.read_bytes()
            self.cliente_github.atualizar(
                self.caminho_repo,
                conteudo,
                fase,
                self._ultimo_confirmado,
            )
            self._ultimo_confirmado = conteudo


def criar_persistidor(caminho: Path, dados: dict, cliente_github=None) -> PersistidorFila:
    if not valor_ativo("PQD_PERSISTENCIA_GITHUB"):
        return PersistidorFila(caminho, dados)
    try:
        relativo = caminho.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        raise RuntimeError("A fila persistida precisa estar dentro do repositório PQD.") from None
    cliente_github = cliente_github or GitHubContentsClient(
        obrigatoria("GITHUB_REPOSITORY"),
        obrigatoria("PQD_GITHUB_CONTENTS_TOKEN"),
        os.getenv("PQD_GITHUB_BRANCH", "main").strip(),
    )
    return PersistidorFila(caminho, dados, cliente_github, relativo)


def baixar_midia(midia: Mapping[str, object], sessao=None) -> Path:
    """Baixa um asset transitório e exige correspondência de hash e tamanho."""
    asset = str(midia.get("asset", "")).strip()
    url = str(midia.get("url_publica", "")).strip()
    digest_esperado = str(midia.get("sha256", "")).strip().casefold()
    try:
        tamanho_esperado = int(midia.get("tamanho_bytes", 0))
    except (TypeError, ValueError):
        raise RuntimeError("tamanho_bytes da mídia é inválido.") from None
    if not asset or Path(asset).name != asset:
        raise RuntimeError("Nome de asset ausente ou inseguro.")
    url_analisada = urlsplit(url)
    if (
        url_analisada.scheme.casefold() != "https"
        or not url_analisada.netloc
        or url_analisada.username is not None
        or url_analisada.password is not None
    ):
        raise RuntimeError("A URL da mídia precisa ser HTTPS e não pode conter credenciais.")
    if not re.fullmatch(r"[0-9a-f]{64}", digest_esperado) or tamanho_esperado <= 0:
        raise RuntimeError("Metadados de integridade da mídia estão incompletos.")

    cache = Path(obrigatoria("PQD_MEDIA_CACHE_DIR")).expanduser().resolve()
    if os.name == "nt":
        raiz_oficial = RAIZ_DRIVE_OFICIAL.resolve()
        if not cache.is_relative_to(raiz_oficial):
            raise RuntimeError(
                "Em execução local, PQD_MEDIA_CACHE_DIR precisa estar sob a raiz oficial "
                r"G:\Meu Drive\PROJETO SEGUNDO CÉREBRO Cristiano Ladik\Projeto - Palavra Que Desperta - Drive."
            )
    cache.mkdir(parents=True, exist_ok=True)
    destino = cache / f"{digest_esperado[:12]}-{asset}"
    temporario = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.partial")
    cliente = sessao or requests
    digest = hashlib.sha256()
    tamanho = 0
    try:
        with cliente.get(url, stream=True, timeout=(30, 900)) as resposta:
            if not resposta.ok:
                raise RuntimeError(
                    f"Não foi possível baixar o asset {asset}: HTTP {resposta.status_code}"
                )
            with temporario.open("xb") as arquivo:
                for bloco in resposta.iter_content(chunk_size=1024 * 1024):
                    if bloco:
                        arquivo.write(bloco)
                        digest.update(bloco)
                        tamanho += len(bloco)
        if digest.hexdigest().casefold() != digest_esperado:
            raise RuntimeError(f"SHA-256 divergente para o asset {asset}.")
        if tamanho != tamanho_esperado:
            raise RuntimeError(f"Tamanho divergente para o asset {asset}.")
        os.replace(temporario, destino)
        return destino
    except Exception as erro:
        temporario.unlink(missing_ok=True)
        destino.unlink(missing_ok=True)
        if isinstance(erro, RuntimeError) and url not in str(erro):
            raise
        raise RuntimeError(
            f"Falha segura ao baixar o asset {asset}: {mensagem_segura(erro, (url,))}"
        ) from None


@dataclass(frozen=True)
class ResultadoRede:
    plataforma: str
    resultado: str
    erro: str | None = None


Publicador = Callable[[MutableMapping[str, object], Callable[[], None]], str]


def executar_plataforma(
    item: MutableMapping[str, object],
    plataforma: str,
    publicador: Publicador,
    persistir: Callable[..., None],
) -> ResultadoRede:
    """Publica uma vez; qualquer ambiguidade posterior exige revisão humana."""
    registro = item.setdefault(plataforma, {})
    if not isinstance(registro, MutableMapping):
        raise RuntimeError(f"Estado inválido para {plataforma}.")
    if plataforma_publicada(registro):
        return ResultadoRede(plataforma, "ja_publicado")

    estado = str(registro.get("status", "pendente"))
    estados_ambiguos = {"publicacao_iniciada", "processando", "erro"}
    marcadores_ambiguos = any(
        registro.get(chave)
        for chave in ("container_id", "video_id", "upload_concluido", "publicacao_iniciada_em")
    )
    if (
        estado in estados_ambiguos
        or marcadores_ambiguos
        or (estado == "publicado" and not registro.get("id"))
    ):
        erro = "Estado de publicação ambíguo; reenvio automático bloqueado."
        registro.update(
            {
                "status": "revisao_manual",
                "erro": erro,
                "atualizado_em": agora_brt().isoformat(),
            }
        )
        persistir(f"{plataforma}:revisao_manual_retomada")
        return ResultadoRede(plataforma, "erro", erro)
    if estado == "revisao_manual":
        return ResultadoRede(plataforma, "erro", redigir_segredos(registro.get("erro", "Revisão manual.")))

    registro.update(
        {
            "status": "publicacao_iniciada",
            "publicacao_iniciada_em": agora_brt().isoformat(),
        }
    )
    registro.pop("erro", None)
    # Este checkpoint durável precede a primeira chamada mutável à Meta.
    persistir(f"{plataforma}:antes_da_mutacao")
    try:
        identificador = str(publicador(item, persistir)).strip()
        if not identificador:
            raise RuntimeError(f"{plataforma} não retornou ID da publicação.")
        registro.update(
            {
                "status": "publicado",
                "id": identificador,
                "publicado_em": agora_brt().isoformat(),
            }
        )
        registro.pop("erro", None)
        persistir(f"{plataforma}:depois_da_confirmacao")
        return ResultadoRede(plataforma, "publicado")
    except Exception as erro_original:
        erro = mensagem_segura(erro_original)
        possivel_id = str(registro.get("id", "")).strip()
        registro.update(
            {
                "status": "revisao_manual",
                "erro": erro,
                "ultima_tentativa_em": agora_brt().isoformat(),
            }
        )
        if possivel_id:
            registro["possivel_id"] = possivel_id
        try:
            persistir(f"{plataforma}:falha_revisao_manual")
        except Exception as erro_persistencia:
            print(
                f"ERRO {plataforma}: revisão manual local registrada, mas checkpoint remoto falhou: "
                f"{mensagem_segura(erro_persistencia)}"
            )
        print(f"ERRO {plataforma}: {erro}")
        return ResultadoRede(plataforma, "erro", erro)


def token_pagina_facebook(cliente, ambiente: Mapping[str, str] | None = None) -> str:
    """Troca a chave do usuario do sistema pela chave da propria Pagina.

    A Meta recusa publicar video na Pagina com a chave do usuario do sistema:
    devolve "(#190) This method must be called with a Page Access Token" e
    "(#200) Subject does not have permission to post videos on this target".
    A chave da Pagina e obtida sob demanda e nunca fica guardada em lugar nenhum.
    """
    ambiente = os.environ if ambiente is None else ambiente
    fornecida = obrigatoria("PQD_FB_PAGE_ACCESS_TOKEN", ambiente)
    page_id = obrigatoria("PQD_FB_PAGE_ID", ambiente)
    obter = getattr(cliente, "get", None)
    if obter is None:
        # Clientes de teste nao expoem GET; nesses casos a chave fornecida basta.
        return fornecida
    resposta = obter(page_id, {"fields": "access_token", "access_token": fornecida})
    return str(resposta.get("access_token") or fornecida)
