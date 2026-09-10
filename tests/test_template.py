from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import automacao_comum as comum
import coordenar
import limpar_release
import politica_agenda
import publicar
import publicar_stories


HASH_A = "a" * 64
HASH_B = "b" * 64
AGORA = datetime(2026, 9, 8, 22, 0, tzinfo=comum.BRT)
ATIVO = {
    "PQD_AUTOMACAO_ATIVA": "true",
    "PQD_PUBLICACAO_CONFIRMADA": "true",
}
META = {
    **ATIVO,
    "PQD_IG_ACCESS_TOKEN": "ig-segredo-123",
    "PQD_IG_BUSINESS_ID": "ig-id",
    "PQD_FB_PAGE_ACCESS_TOKEN": "fb-segredo-456",
    "PQD_FB_PAGE_ID": "page-id",
    "PQD_GITHUB_CONTENTS_TOKEN": "gh-segredo-789",
}


def midia(asset: str = "asset.mp4", digest: str = HASH_A, duracao: float = 30.0) -> dict:
    return {
        "asset": asset,
        "url_publica": (
            "https://github.com/dono/repo/releases/download/"
            f"fila-instagram-facebook/{asset}"
        ),
        "sha256": digest,
        "tamanho_bytes": 1,
        "duracao_segundos": duracao,
    }


def reel(numero: int, horario: str = "09:00", data: str = "2026-09-08") -> dict:
    return {
        "id": f"reel-{numero}",
        "data": data,
        "horario": horario,
        "status": "pendente",
        "midia": midia(f"reel-{numero}.mp4", f"{numero:064x}"),
        "instagram": {
            "legenda": "siga @palavraquedesperta_br",
            "status": "pendente",
        },
        "facebook": {
            "legenda": "siga @palavraquedesperta_br",
            "status": "pendente",
        },
    }


def parte(ordem: int, duracao: float = 30.0, asset: str | None = None, digest: str | None = None) -> dict:
    return {
        "ordem": ordem,
        "status": "pendente",
        "midia": midia(asset or f"parte-{ordem:02d}.mp4", digest or f"{ordem:064x}", duracao),
        "instagram": {"status": "pendente"},
        "facebook": {"status": "pendente"},
    }


def pacote(quantidade: int = 2, data: str = "2026-09-08", horario: str = "09:00") -> dict:
    return {
        "id": f"pacote-{data}",
        "data": data,
        "horario": horario,
        "status": "pendente",
        "partes": [parte(i) for i in range(1, quantidade + 1)],
    }


def fila_reels(itens: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "canal": "instagram-facebook-reels",
        "timezone": "America/Sao_Paulo",
        "conteudos": itens,
    }


def fila_stories(pacotes: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "canal": "instagram-facebook-stories",
        "timezone": "America/Sao_Paulo",
        "limite_parte_segundos": 59,
        "pacotes": pacotes,
    }


def gravar_json(caminho: Path, dados: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")


def politica(inicio: str | None = "2026-09-08") -> dict:
    dados = comum.carregar_json(ROOT / "politica_publicacao.json")
    dados["publicacao_habilitada"] = True
    dados["autoridade_unica_confirmada"] = True
    dados["data_inicio_rampa"] = inicio
    dados["repositorio_esperado"] = "dono/repo"
    dados["github_visibilidade"] = "publico"
    dados["exposicao_midias_futuras_confirmada"] = True
    return dados


class CacheFalso:
    def __init__(self, pasta: Path, falhar_em: int | None = None) -> None:
        self.pasta = pasta
        self.falhar_em = falhar_em
        self.chamadas = 0
        self.caminhos: list[Path] = []

    def __call__(self, _midia: dict) -> Path:
        self.chamadas += 1
        if self.chamadas == self.falhar_em:
            raise RuntimeError("asset divergente")
        caminho = self.pasta / f"cache-{self.chamadas}.teste"
        caminho.write_text("validado", encoding="utf-8")
        self.caminhos.append(caminho)
        return caminho


class Resposta:
    def __init__(self, dados=None, ok=True, status=200, texto="") -> None:
        self._dados = {} if dados is None else dados
        self.ok = ok
        self.status_code = status
        self.text = texto

    def json(self):
        return self._dados


class MetaPreflightFalsa:
    def __init__(self, username="palavraquedesperta_br", ligada=True) -> None:
        self.username = username
        self.ligada = ligada
        self.chamadas: list[tuple[str, dict]] = []
        self.sessao = object()

    def get(self, caminho: str, parametros: dict) -> dict:
        self.chamadas.append((caminho, parametros))
        if caminho == "ig-id":
            return {"id": "ig-id", "username": self.username}
        ligada = (
            {"id": "ig-id", "username": "palavraquedesperta_br"}
            if self.ligada
            else {"id": "outro-ig", "username": "outra_conta"}
        )
        return {"id": "page-id", "name": "PQD", "instagram_business_account": ligada}


class MetaQuotaFalsa:
    def __init__(self, respostas=None) -> None:
        self.respostas = list(respostas or [])
        self.get_calls: list[tuple[str, dict]] = []
        self.sessao = object()

    def get(self, caminho: str, parametros: dict) -> dict:
        self.get_calls.append((caminho, parametros))
        return self.respostas.pop(0) if self.respostas else {}


class ReleaseFalsa:
    def __init__(self, assets: list[dict]) -> None:
        self._assets = assets
        self.excluidos: list[int] = []

    def assets(self, _tag: str) -> list[dict]:
        return self._assets

    def excluir(self, asset_id: int) -> None:
        self.excluidos.append(asset_id)


class ComumTests(unittest.TestCase):
    def test_duas_travas_sao_obrigatorias(self) -> None:
        self.assertFalse(comum.automacao_ativa({}))
        self.assertFalse(comum.automacao_ativa({"PQD_AUTOMACAO_ATIVA": "true"}))
        self.assertFalse(comum.automacao_ativa({"PQD_PUBLICACAO_CONFIRMADA": "true"}))
        self.assertTrue(comum.automacao_ativa(ATIVO))

    def test_persistencia_duravel_e_obrigatoria_na_ativacao_real(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PQD_PERSISTENCIA_GITHUB=true"):
            comum.exigir_persistencia_duravel(ATIVO)
        comum.exigir_persistencia_duravel({**ATIVO, "PQD_PERSISTENCIA_GITHUB": "true"})

    def test_graph_padrao_v26(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(comum.MetaClient(sessao=object()).versao, "v26.0")

    def test_preflight_exige_as_quatro_credenciais_antes_de_get(self) -> None:
        cliente = MetaPreflightFalsa()
        with patch.dict(os.environ, ATIVO, clear=True):
            with self.assertRaisesRegex(RuntimeError, "PQD_IG_ACCESS_TOKEN"):
                comum.validar_preflight_meta(cliente)
        self.assertEqual(cliente.chamadas, [])

    def test_preflight_valida_username_e_page_ligada_com_dois_gets(self) -> None:
        cliente = MetaPreflightFalsa()
        with patch.dict(os.environ, META, clear=True):
            resultado = comum.validar_preflight_meta(cliente)
        self.assertEqual(resultado.instagram_username, "palavraquedesperta_br")
        self.assertEqual([item[0] for item in cliente.chamadas], ["ig-id", "page-id"])

    def test_preflight_recusa_username_ou_vinculo_errado(self) -> None:
        with patch.dict(os.environ, META, clear=True):
            with self.assertRaisesRegex(RuntimeError, "não é @palavraquedesperta_br"):
                comum.validar_preflight_meta(MetaPreflightFalsa(username="intruso"))
            with self.assertRaisesRegex(RuntimeError, "não está ligada"):
                comum.validar_preflight_meta(MetaPreflightFalsa(ligada=False))

    def test_get_post_e_upload_redigem_token_em_excecoes(self) -> None:
        segredo = META["PQD_IG_ACCESS_TOKEN"]

        class Sessao:
            def get(self, *_args, **_kwargs):
                raise RuntimeError(f"falhou com {segredo}")

            def post(self, *_args, **_kwargs):
                raise RuntimeError(f"falhou com {segredo}")

        cliente = comum.MetaClient(sessao=Sessao())
        with patch.dict(os.environ, META, clear=True):
            for chamada in (
                lambda: cliente.get("id", {"access_token": segredo}),
                lambda: cliente.post("id", {"access_token": segredo}),
            ):
                with self.assertRaises(comum.MetaErro) as contexto:
                    chamada()
                self.assertNotIn(segredo, str(contexto.exception))
            with tempfile.TemporaryDirectory() as pasta:
                video = Path(pasta) / "v.mp4"
                video.write_bytes(b"x")
                with self.assertRaises(comum.MetaErro) as contexto:
                    cliente.upload("https://upload.invalid/assinado", segredo, video)
                self.assertNotIn(segredo, str(contexto.exception))
                self.assertNotIn("assinado", str(contexto.exception))

    def test_resposta_meta_redige_token_do_corpo(self) -> None:
        segredo = META["PQD_IG_ACCESS_TOKEN"]

        class Sessao:
            def post(self, *_args, **_kwargs):
                return Resposta(ok=False, status=400, texto=f"token={segredo}")

        with patch.dict(os.environ, META, clear=True):
            with self.assertRaises(comum.MetaErro) as contexto:
                comum.MetaClient(sessao=Sessao()).post("id", {"access_token": segredo})
        self.assertNotIn(segredo, str(contexto.exception))

    def test_json_persistido_redige_segredos(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            comum.salvar_json_atomico(caminho, {"erro": f"falha {META['PQD_IG_ACCESS_TOKEN']}"})
            self.assertNotIn(META["PQD_IG_ACCESS_TOKEN"], caminho.read_text(encoding="utf-8"))
            self.assertIn("<redigido>", caminho.read_text(encoding="utf-8"))

    def test_checkpoint_contents_api_usa_sha_e_nao_persiste_credencial(self) -> None:
        class Sessao:
            def __init__(self):
                self.payload = None

            def get(self, *_args, **_kwargs):
                return Resposta({"sha": "sha-atual", "content": base64.b64encode(b"").decode("ascii")})

            def put(self, _url, **kwargs):
                self.payload = kwargs["json"]
                return Resposta({"commit": {"sha": "novo"}})

        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            sessao = Sessao()
            cliente = comum.GitHubContentsClient("dono/repo", META["PQD_GITHUB_CONTENTS_TOKEN"], sessao=sessao)
            persistir = comum.PersistidorFila(
                caminho,
                {"erro": f"nunca salvar {META['PQD_GITHUB_CONTENTS_TOKEN']}"},
                cliente,
                "fila/fila-reels.json",
            )
            persistir("antes_da_mutacao")
            self.assertEqual(sessao.payload["sha"], "sha-atual")
            conteudo = base64.b64decode(sessao.payload["content"]).decode("utf-8")
            self.assertNotIn(META["PQD_GITHUB_CONTENTS_TOKEN"], conteudo)
            self.assertNotIn(META["PQD_GITHUB_CONTENTS_TOKEN"], json.dumps(sessao.payload))

    def test_checkpoint_recusa_sobrescrever_fila_remota_alterada(self) -> None:
        class Sessao:
            put_chamado = False

            def get(self, *_args, **_kwargs):
                remoto = b'{"alterado":"por-outro-processo"}\n'
                return Resposta({"sha": "sha-novo", "content": base64.b64encode(remoto).decode("ascii")})

            def put(self, *_args, **_kwargs):
                self.put_chamado = True
                return Resposta()

        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            caminho.write_bytes(b'{"original":true}\n')
            sessao = Sessao()
            persistir = comum.PersistidorFila(
                caminho,
                {"original": True, "estado": "novo"},
                comum.GitHubContentsClient("dono/repo", "token", sessao=sessao),
                "fila/fila-reels.json",
            )
            with self.assertRaisesRegex(RuntimeError, "Conflito no checkpoint"):
                persistir("antes_da_mutacao")
            self.assertFalse(sessao.put_chamado)

    def test_publicacao_iniciada_precede_chamada_e_falha_vira_revisao(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            item = reel(1)
            fila = {"conteudos": [item]}
            persistir = comum.PersistidorFila(caminho, fila)
            estados: list[str] = []

            def falhar(atual, _checkpoint):
                estados.append(comum.carregar_json(caminho)["conteudos"][0]["instagram"]["status"])
                raise RuntimeError(f"falha {META['PQD_IG_ACCESS_TOKEN']}")

            resultado = comum.executar_plataforma(item, "instagram", falhar, persistir)
            atual = comum.carregar_json(caminho)["conteudos"][0]["instagram"]
            self.assertEqual(estados, ["publicacao_iniciada"])
            self.assertEqual(resultado.resultado, "erro")
            self.assertEqual(atual["status"], "revisao_manual")
            self.assertNotIn(META["PQD_IG_ACCESS_TOKEN"], atual["erro"])

    def test_estado_ambiguo_nunca_reenvia(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "fila.json"
            item = reel(1)
            item["instagram"] = {"status": "publicacao_iniciada"}
            fila = {"conteudos": [item]}
            comum.salvar_json_atomico(caminho, fila)
            chamadas: list[int] = []
            resultado = comum.executar_plataforma(
                item,
                "instagram",
                lambda *_: chamadas.append(1) or "id",
                comum.PersistidorFila(caminho, fila),
            )
            self.assertEqual(resultado.resultado, "erro")
            self.assertEqual(chamadas, [])
            self.assertEqual(item["instagram"]["status"], "revisao_manual")

    def test_download_exige_https_e_cache_namespaced(self) -> None:
        dados = midia()
        dados["url_publica"] = "http://example.invalid/asset.mp4"
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "HTTPS"):
            comum.baixar_midia(dados, sessao=object())
        dados["url_publica"] = "https://example.invalid/asset.mp4"
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "PQD_MEDIA_CACHE_DIR"):
            comum.baixar_midia(dados, sessao=object())
        if os.name == "nt":
            with patch.dict(
                os.environ,
                {"PQD_MEDIA_CACHE_DIR": r"G:\Outro Projeto\cache"},
                clear=True,
            ), self.assertRaisesRegex(RuntimeError, "raiz oficial"):
                comum.baixar_midia(dados, sessao=object())


class PoliticaTests(unittest.TestCase):
    def test_data_inicio_null_bloqueia_ativacao(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            regra = politica(None)
            regra["publicacao_habilitada"] = True
            regra["autoridade_unica_confirmada"] = True
            caminho = Path(pasta) / "politica.json"
            gravar_json(caminho, regra)
            with self.assertRaisesRegex(politica_agenda.PoliticaErro, "data_inicio_rampa está null"):
                politica_agenda.carregar_politica(caminho)

    def test_flags_versionadas_e_invariantes_da_rampa_bloqueiam_alteracao(self) -> None:
        # A checagem da politica real do repositorio saiu daqui em 10/09/2026, quando
        # a publicacao foi ligada. O que este teste protege de verdade e a logica
        # abaixo, com politicas de mentira: trava desligada e teto de rampa alterado.
        with tempfile.TemporaryDirectory() as pasta:
            regra = politica()
            regra["publicacao_habilitada"] = True
            regra["autoridade_unica_confirmada"] = False
            caminho = Path(pasta) / "politica.json"
            gravar_json(caminho, regra)
            with self.assertRaisesRegex(politica_agenda.PoliticaErro, "autoridade_unica_confirmada=false"):
                politica_agenda.carregar_politica(caminho)
            regra["autoridade_unica_confirmada"] = True
            regra["reels"]["maximo_diario_por_semana"]["1"] = 10
            gravar_json(caminho, regra)
            with self.assertRaisesRegex(politica_agenda.PoliticaErro, "tetos 1/2/3/4/5"):
                politica_agenda.carregar_politica(caminho)

    def test_rampa_valida_por_semana(self) -> None:
        fila = fila_reels(
            [
                reel(1, "09:00", "2026-09-08"),
                reel(2, "09:00", "2026-09-15"),
                reel(3, "21:00", "2026-09-15"),
                reel(4, "05:00", "2026-10-06"),
                reel(5, "09:00", "2026-10-06"),
                reel(6, "13:00", "2026-10-06"),
                reel(7, "17:00", "2026-10-06"),
                reel(8, "21:00", "2026-10-06"),
            ]
        )
        politica_agenda.validar_fila_reels(fila, politica())

    def test_bloqueia_slot_fora_da_rampa(self) -> None:
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "fora da rampa"):
            politica_agenda.validar_fila_reels(
                fila_reels([reel(1, "05:00", "2026-09-08")]), politica()
            )

    def test_bloqueia_duplicidade(self) -> None:
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "duplicados"):
            politica_agenda.validar_fila_reels(
                fila_reels([reel(1), reel(2)]), politica()
            )

    def test_bloqueia_teto_diario_real(self) -> None:
        regra = politica()
        regra["reels"]["horarios_por_semana"]["5+"] = [
            "05:00", "09:00", "11:00", "13:00", "17:00", "21:00"
        ]
        itens = [
            reel(i, horario, "2026-10-06")
            for i, horario in enumerate(regra["reels"]["horarios_por_semana"]["5+"], 1)
        ]
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "Teto diário"):
            politica_agenda.validar_fila_reels(fila_reels(itens), regra)

    def test_story_somente_09_e_um_pacote_fonte(self) -> None:
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "somente às 09:00"):
            politica_agenda.validar_fila_stories(
                fila_stories([pacote(1, horario="10:00")]), politica()
            )
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "Mais de um pacote-fonte"):
            politica_agenda.validar_fila_stories(
                fila_stories([pacote(1), {**pacote(1), "id": "outro"}]), politica()
            )
        desigual = pacote(2)
        desigual["partes"][1]["midia"]["duracao_segundos"] = 31.0
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "partes iguais"):
            politica_agenda.validar_fila_stories(fila_stories([desigual]), politica())

    def test_cabecalhos_e_release_sao_obrigatorios(self) -> None:
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "schema_version"):
            politica_agenda.validar_fila_reels({"conteudos": []}, politica())
        fila = fila_reels([reel(1)])
        fila["conteudos"][0]["midia"]["url_publica"] = "https://example.invalid/video.mp4"
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "Release"):
            politica_agenda.validar_fila_reels(fila, politica())
        stories = fila_stories([pacote(1)])
        stories["limite_parte_segundos"] = 60
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "limite_parte"):
            politica_agenda.validar_fila_stories(stories, politica())

    def test_duracao_nao_finita_e_ordem_invalida_sao_bloqueadas(self) -> None:
        item = reel(1)
        item["midia"]["duracao_segundos"] = float("nan")
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "duração 3–180"):
            politica_agenda.validar_fila_reels(fila_reels([item]), politica())
        pacote_ruim = pacote(2)
        pacote_ruim["partes"][1]["ordem"] = 3
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "ordens contíguas"):
            politica_agenda.validar_fila_stories(
                fila_stories([pacote_ruim]), politica()
            )

    def test_tolerancia_story_alinhada_em_vinte_e_cinco_centesimos(self) -> None:
        dentro = pacote(2)
        dentro["partes"][1]["midia"]["duracao_segundos"] = 30.25
        politica_agenda.validar_fila_stories(fila_stories([dentro]), politica())
        fora = pacote(2)
        fora["partes"][1]["midia"]["duracao_segundos"] = 30.251
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "partes iguais"):
            politica_agenda.validar_fila_stories(fila_stories([fora]), politica())

    def test_manual_nunca_publica_futuro(self) -> None:
        futuro = datetime(2026, 9, 9, 5, 0, tzinfo=comum.BRT)
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "futuro"):
            politica_agenda.validar_manual_nao_futuro(futuro, AGORA)
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "futuro"):
            publicar.itens_devidos(
                {"conteudos": [reel(1, "05:00", "2026-09-09")]},
                AGORA,
                data_forcada="2026-09-09",
                horario_forcado="05:00",
            )

    def test_backlog_semana_um_reserva_so_um_novo_reel_por_rede(self) -> None:
        agora = datetime(2026, 9, 14, 22, 0, tzinfo=comum.BRT)
        regra = politica("2026-09-08")
        primeiro = reel(1, "09:00", "2026-09-08")
        segundo = reel(2, "09:00", "2026-09-09")
        selecionados, teto = politica_agenda.limitar_reels_pelo_teto_real(
            [primeiro, segundo], {"conteudos": [primeiro, segundo]}, regra, agora
        )
        self.assertEqual([item["id"] for item in selecionados], ["reel-1"])
        self.assertEqual(teto["limite"], 1)

    def test_publicado_hoje_consumiu_saldo_da_rede_no_backlog(self) -> None:
        regra = politica("2026-09-08")
        publicado = reel(1)
        publicado["instagram"] = {
            "status": "publicado",
            "id": "ig-1",
            "publicado_em": "2026-09-08T08:00:00-03:00",
        }
        publicado["facebook"] = {
            "status": "publicado",
            "id": "fb-1",
            "publicado_em": "2026-09-08T08:01:00-03:00",
        }
        pendente = reel(2)
        selecionados, teto = politica_agenda.limitar_reels_pelo_teto_real(
            [pendente], {"conteudos": [publicado, pendente]}, regra, AGORA
        )
        self.assertEqual(selecionados, [])
        self.assertEqual(teto["usados_instagram"], 1)
        self.assertEqual(teto["usados_facebook"], 1)

    def test_story_backlog_nao_inicia_segundo_pacote_no_dia_real_mas_retomada_pode(self) -> None:
        regra = politica("2026-09-01")
        primeiro = pacote(1, data="2026-09-07")
        primeiro["status"] = "concluido"
        primeiro["publicacao_iniciada_em"] = "2026-09-08T09:00:00-03:00"
        segundo = pacote(1, data="2026-09-08")
        fila = {"pacotes": [primeiro, segundo]}
        self.assertFalse(
            politica_agenda.story_pode_iniciar_no_dia_real(segundo, fila, regra, AGORA)
        )
        segundo["publicacao_iniciada_em"] = "2026-09-08T10:00:00-03:00"
        self.assertTrue(
            politica_agenda.story_pode_iniciar_no_dia_real(segundo, fila, regra, AGORA)
        )

    def test_coerencia_bloqueia_repo_nulo_ou_env_divergente(self) -> None:
        regra = politica()
        regra["repositorio_esperado"] = None
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "repositorio_esperado está null"):
            politica_agenda.validar_coerencia_ambiente(regra, {})
        regra["repositorio_esperado"] = "dono/repo"
        ambiente = {
            "GITHUB_REPOSITORY": "outro/repo",
            "PQD_META_GRAPH_VERSION": "v26.0",
            "PQD_RELEASE_TAG": "fila-instagram-facebook",
            "PQD_GITHUB_VISIBILIDADE_ATUAL": "public",
        }
        with self.assertRaisesRegex(politica_agenda.PoliticaErro, "GITHUB_REPOSITORY"):
            politica_agenda.validar_coerencia_ambiente(regra, ambiente)
        ambiente["GITHUB_REPOSITORY"] = "dono/repo"
        politica_agenda.validar_coerencia_ambiente(regra, ambiente)


class CoordenadorTests(unittest.TestCase):
    def _filas_vazias(self, raiz: Path) -> tuple[Path, Path]:
        reels = raiz / "fila-reels.json"
        stories = raiz / "fila-stories.json"
        gravar_json(reels, fila_reels([]))
        gravar_json(stories, fila_stories([]))
        return reels, stories

    def test_politica_incompleta_bloqueia_antes_do_primeiro_get_meta(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(
            os.environ,
            {**META, "PQD_PERSISTENCIA_GITHUB": "true"},
            clear=True,
        ):
            reels, stories = self._filas_vazias(Path(pasta))
            cliente = MetaPreflightFalsa()
            with patch.object(coordenar, "FILA_REELS", reels), patch.object(
                coordenar, "FILA_STORIES", stories
            ):
                # O que importa aqui e que a politica barra ANTES de falar com a Meta.
                # A mensagem exata mudou quando a publicacao foi ligada em 10/09/2026,
                # entao o teste checa o bloqueio, nao o texto.
                with self.assertRaises(politica_agenda.PoliticaErro):
                    coordenar.executar(cliente)
            self.assertEqual(cliente.chamadas, [])

    def test_stories_tem_prioridade_reels_continua_independente_e_limpeza_bloqueia(self) -> None:
        ordem: list[str] = []
        politicas_recebidas: list[dict | None] = []
        with tempfile.TemporaryDirectory() as pasta, patch.dict(
            os.environ,
            {**META, "PQD_PERSISTENCIA_GITHUB": "true"},
            clear=True,
        ):
            reels, stories = self._filas_vazias(Path(pasta))
            with patch.object(coordenar, "FILA_REELS", reels), patch.object(
                coordenar, "FILA_STORIES", stories
            ), patch.object(coordenar, "carregar_politica", return_value=politica()), patch.object(
                coordenar, "validar_coerencia_ambiente"
            ), patch.object(coordenar, "validar_preflight_meta"), patch.object(
                coordenar,
                "publicar_stories",
                side_effect=lambda **kwargs: (
                    ordem.append("stories"), politicas_recebidas.append(kwargs.get("politica_execucao")), 1
                )[-1],
            ), patch.object(
                coordenar,
                "publicar_reels",
                side_effect=lambda **kwargs: (
                    ordem.append("reels"), politicas_recebidas.append(kwargs.get("politica_execucao")), 0
                )[-1],
            ), patch.object(
                coordenar,
                "limpar",
                side_effect=lambda *_args, **_kwargs: ordem.append("limpar") or 0,
            ):
                self.assertEqual(coordenar.executar(object()), 1)
        self.assertEqual(ordem, ["stories", "reels"])
        self.assertEqual(len(politicas_recebidas), 2)
        self.assertTrue(all(item is not None for item in politicas_recebidas))


class ReelsTests(unittest.TestCase):
    def test_flag_politica_validada_sem_objeto_falha_fechado(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            gravar_json(caminho, {"conteudos": []})
            with self.assertRaisesRegex(RuntimeError, "politica_execucao"):
                publicar.executar(
                    caminho,
                    preflight_realizado=True,
                    politica_validada=True,
                    requisitos_ativacao_validados=True,
                )

    def test_desativado_nao_muda_fila_nem_chama_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "fila.json"
            gravar_json(caminho, {"conteudos": [reel(1)]})
            antes = caminho.read_bytes()
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(publicar.executar(caminho, cliente=object()), 0)
            self.assertEqual(caminho.read_bytes(), antes)

    def test_preflight_midia_falha_sem_publicador(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            gravar_json(caminho, {"conteudos": [reel(1), reel(2, "21:00")]})
            chamados: list[str] = []
            codigo = publicar.executar(
                caminho,
                cliente=object(),
                agora=AGORA,
                publicador_instagram=lambda *_: chamados.append("ig") or "ig-id",
                publicador_facebook=lambda *_: chamados.append("fb") or "fb-id",
                validador_midia=lambda _: (_ for _ in ()).throw(RuntimeError("hash ruim")),
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
                politica_execucao=politica(),
            )
            self.assertEqual(codigo, 1)
            self.assertEqual(chamados, [])
            self.assertEqual(comum.carregar_json(caminho)["conteudos"][0]["status"], "erro_midia")

    def test_falha_instagram_vira_revisao_e_nao_inicia_facebook_ou_proximo(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            raiz = Path(pasta)
            caminho = raiz / "fila.json"
            gravar_json(caminho, {"conteudos": [reel(1), reel(2, "21:00")]})
            chamadas: list[str] = []

            def ig(item, _checkpoint):
                chamadas.append(f"ig-{item['id']}")
                raise RuntimeError("resultado ambíguo")

            codigo = publicar.executar(
                caminho,
                cliente=object(),
                agora=AGORA,
                publicador_instagram=ig,
                publicador_facebook=lambda item, _: chamadas.append(f"fb-{item['id']}") or "fb",
                validador_midia=CacheFalso(raiz),
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
                politica_execucao=politica(),
            )
            atual = comum.carregar_json(caminho)["conteudos"]
            self.assertEqual(codigo, 1)
            self.assertEqual(chamadas, ["ig-reel-1"])
            self.assertEqual(atual[0]["instagram"]["status"], "revisao_manual")
            self.assertEqual(atual[0]["facebook"]["status"], "pendente")
            self.assertEqual(atual[1]["status"], "pendente")

    def test_rede_ja_publicada_nao_repete_e_conclui_a_outra(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            raiz = Path(pasta)
            caminho = raiz / "fila.json"
            item = reel(1)
            item["instagram"] = {
                "status": "publicado",
                "id": "ig-existente",
                "publicado_em": "2026-09-08T08:00:00-03:00",
            }
            gravar_json(caminho, {"conteudos": [item]})
            chamadas_ig: list[int] = []
            codigo = publicar.executar(
                caminho,
                cliente=object(),
                agora=AGORA,
                publicador_instagram=lambda *_: chamadas_ig.append(1) or "duplicado",
                publicador_facebook=lambda *_: "fb-novo",
                validador_midia=CacheFalso(raiz),
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
                politica_execucao=politica(),
            )
            atual = comum.carregar_json(caminho)["conteudos"][0]
            self.assertEqual(codigo, 0)
            self.assertEqual(chamadas_ig, [])
            self.assertEqual(atual["status"], "concluido")

    def test_legenda_exata_nas_duas_redes(self) -> None:
        class ClienteIG:
            def __init__(self):
                self.posts = []

            def post(self, caminho, dados):
                self.posts.append((caminho, dados))
                return {"id": "container"} if caminho.endswith("/media") else {"id": "ig-final"}

            def aguardar_instagram(self, *_):
                return None

        with patch.dict(os.environ, META, clear=True):
            item = reel(1)
            cliente = ClienteIG()
            publicar.publicar_instagram_reel(item, cliente, lambda: None)
            self.assertEqual(cliente.posts[0][1]["caption"], "siga @palavraquedesperta_br")

            class ClienteFB:
                def __init__(self):
                    self.posts = []

                def post(self, caminho, dados):
                    self.posts.append((caminho, dados))
                    return {"success": True}

            item["facebook"] = {"status": "publicacao_iniciada", "video_id": "v", "upload_concluido": True}
            fb = ClienteFB()
            publicar.publicar_facebook_reel(item, fb, lambda: None)
            self.assertEqual(fb.posts[0][1]["description"], "siga @palavraquedesperta_br")


class StoriesTests(unittest.TestCase):
    def test_flag_politica_validada_sem_objeto_falha_fechado(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            gravar_json(caminho, {"pacotes": []})
            with self.assertRaisesRegex(RuntimeError, "politica_execucao"):
                publicar_stories.executar(
                    caminho,
                    preflight_realizado=True,
                    politica_validada=True,
                    requisitos_ativacao_validados=True,
                )

    def test_rejeita_parte_acima_de_59_sem_download(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminho = Path(pasta) / "fila.json"
            ruim = pacote(1)
            ruim["partes"][0]["midia"]["duracao_segundos"] = 59.001
            gravar_json(caminho, {"pacotes": [ruim]})
            cache = CacheFalso(Path(pasta))
            codigo = publicar_stories.executar(
                caminho,
                cliente=MetaQuotaFalsa(),
                agora=AGORA,
                validador_midia=cache,
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
                politica_execucao=politica(),
            )
            self.assertEqual(codigo, 1)
            self.assertEqual(cache.chamadas, 0)

    def test_preflight_integral_falha_antes_da_meta(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            raiz = Path(pasta)
            caminho = raiz / "fila.json"
            gravar_json(caminho, {"pacotes": [pacote(3)]})
            cliente = MetaQuotaFalsa()
            codigo = publicar_stories.executar(
                caminho,
                cliente=cliente,
                agora=AGORA,
                validador_midia=CacheFalso(raiz, falhar_em=2),
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
                politica_execucao=politica(),
            )
            self.assertEqual(codigo, 1)
            self.assertEqual(cliente.get_calls, [])

    def test_cota_insuficiente_nao_publica(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            raiz = Path(pasta)
            caminho = raiz / "fila.json"
            gravar_json(caminho, {"pacotes": [pacote(3)]})
            cliente = MetaQuotaFalsa([{"data": [{"quota_usage": 24, "config": {"quota_total": 25}}]}])
            chamadas: list[str] = []
            codigo = publicar_stories.executar(
                caminho,
                cliente=cliente,
                agora=AGORA,
                validador_midia=CacheFalso(raiz),
                publicador_instagram=lambda *_: chamadas.append("ig") or "id",
                publicador_facebook=lambda *_: chamadas.append("fb") or "id",
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
                politica_execucao=politica(),
            )
            self.assertEqual(codigo, 0)
            self.assertEqual(chamadas, [])
            self.assertEqual(comum.carregar_json(caminho)["pacotes"][0]["status"], "aguardando_cota")

    def test_falha_de_parte_bloqueia_reenvio_e_parte_posterior(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            raiz = Path(pasta)
            caminho = raiz / "fila.json"
            gravar_json(caminho, {"pacotes": [pacote(3)]})
            cliente = MetaQuotaFalsa([{"data": [{"quota_usage": 0, "config": {"quota_total": 25}}]}])
            chamadas: list[str] = []

            def ig(item, _):
                chamadas.append(f"ig-{item['ordem']}")
                if item["ordem"] == 2:
                    raise RuntimeError("falha ambígua")
                return f"ig-{item['ordem']}"

            codigo = publicar_stories.executar(
                caminho,
                cliente=cliente,
                agora=AGORA,
                validador_midia=CacheFalso(raiz),
                publicador_instagram=ig,
                publicador_facebook=lambda item, _: chamadas.append(f"fb-{item['ordem']}") or f"fb-{item['ordem']}",
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
                politica_execucao=politica(),
            )
            atual = comum.carregar_json(caminho)["pacotes"][0]
            self.assertEqual(codigo, 1)
            self.assertEqual(chamadas, ["ig-1", "fb-1", "ig-2"])
            self.assertEqual(atual["status"], "revisao_manual")
            self.assertEqual(atual["partes"][1]["instagram"]["status"], "revisao_manual")
            self.assertEqual(atual["partes"][2]["status"], "pendente")

    def test_stories_nao_enviam_caption_ou_description(self) -> None:
        class ClienteIG:
            def __init__(self):
                self.posts = []

            def post(self, caminho, dados):
                self.posts.append((caminho, dados))
                return {"id": "container"} if caminho.endswith("/media") else {"id": "story"}

            def aguardar_instagram(self, *_):
                return None

        with patch.dict(os.environ, META, clear=True):
            item = parte(1)
            ig = ClienteIG()
            publicar_stories.publicar_instagram_story(item, ig, lambda: None)
            self.assertNotIn("caption", ig.posts[0][1])

            class ClienteFB:
                def __init__(self):
                    self.posts = []

                def post(self, caminho, dados):
                    self.posts.append((caminho, dados))
                    return {"success": True, "post_id": "fb-story"}

            item["facebook"] = {"status": "publicacao_iniciada", "video_id": "v", "upload_concluido": True}
            fb = ClienteFB()
            publicar_stories.publicar_facebook_story(item, fb, lambda: None)
            self.assertNotIn("description", fb.posts[0][1])


class LimpezaTests(unittest.TestCase):
    @staticmethod
    def item_limpeza(asset="asset.mp4", digest=HASH_A, concluido=True):
        item = {
            "status": "concluido" if concluido else "pendente",
            "midia": midia(asset, digest),
            "instagram": {"status": "publicado", "id": "ig"} if concluido else {"status": "pendente"},
            "facebook": {"status": "publicado", "id": "fb"} if concluido else {"status": "pendente"},
        }
        return item

    def _filas(self, raiz: Path, reels: list[dict], partes: list[dict]):
        reels_path = raiz / "fila-reels.json"
        stories_path = raiz / "fila-stories.json"
        gravar_json(reels_path, {"conteudos": reels})
        gravar_json(stories_path, {"pacotes": [{"partes": partes}] if partes else []})
        return reels_path, stories_path

    def test_referencia_pendente_na_outra_fila_bloqueia(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminhos = self._filas(
                Path(pasta),
                [self.item_limpeza("mesmo.mp4", HASH_A, True)],
                [self.item_limpeza("mesmo.mp4", HASH_B, False)],
            )
            cliente = ReleaseFalsa([{"id": 8, "name": "mesmo.mp4", "digest": f"sha256:{HASH_A}", "size": 1}])
            codigo = limpar_release.executar(
                caminhos,
                cliente,
                "dono/repo",
                "tag",
                cliente_meta=object(),
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
            )
            self.assertEqual(codigo, 1)
            self.assertEqual(cliente.excluidos, [])

    def test_asset_compartilhado_concluido_exclui_uma_vez_e_persiste_ambas(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminhos = self._filas(
                Path(pasta),
                [self.item_limpeza()],
                [self.item_limpeza()],
            )
            cliente = ReleaseFalsa([{"id": 7, "name": "asset.mp4", "digest": f"sha256:{HASH_A}", "size": 1}])
            codigo = limpar_release.executar(
                caminhos,
                cliente,
                "dono/repo",
                "tag",
                cliente_meta=object(),
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
            )
            self.assertEqual(codigo, 0)
            self.assertEqual(cliente.excluidos, [7])
            reel_midia = comum.carregar_json(caminhos[0])["conteudos"][0]["midia"]
            story_midia = comum.carregar_json(caminhos[1])["pacotes"][0]["partes"][0]["midia"]
            self.assertIn("removido_da_release_em", reel_midia)
            self.assertIn("removido_da_release_em", story_midia)

    def test_metadados_inconsistentes_entre_filas_bloqueiam(self) -> None:
        with tempfile.TemporaryDirectory() as pasta, patch.dict(os.environ, META, clear=True):
            caminhos = self._filas(
                Path(pasta),
                [self.item_limpeza("mesmo.mp4", HASH_A, True)],
                [self.item_limpeza("mesmo.mp4", HASH_B, True)],
            )
            cliente = ReleaseFalsa([{"id": 9, "name": "mesmo.mp4", "digest": f"sha256:{HASH_A}", "size": 1}])
            codigo = limpar_release.executar(
                caminhos,
                cliente,
                "dono/repo",
                "tag",
                cliente_meta=object(),
                preflight_realizado=True,
                politica_validada=True,
                requisitos_ativacao_validados=True,
            )
            self.assertEqual(codigo, 1)
            self.assertEqual(cliente.excluidos, [])


class EstruturaTests(unittest.TestCase):
    def test_filas_tem_formato_valido(self) -> None:
        # Ate 10/09/2026 este teste exigia fila vazia, para provar que nada estava
        # ligado. Com a automacao no ar a fila tem conteudo, entao o que se verifica
        # agora e o formato: listas de itens, cada um com data e horario.
        reels = comum.carregar_json(ROOT / "fila" / "fila-reels.json")["conteudos"]
        stories = comum.carregar_json(ROOT / "fila" / "fila-stories.json")["pacotes"]
        self.assertIsInstance(reels, list)
        self.assertIsInstance(stories, list)
        for item in list(reels) + list(stories):
            self.assertIn("data", item)
            self.assertIn("horario", item)

    def test_workflow_unico_pinado_e_sem_credencial_git(self) -> None:
        workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertEqual([item.name for item in workflows], ["publicar-meta.yml"])
        texto = workflows[0].read_text(encoding="utf-8")
        self.assertIn('cron: "*/15 * * * *"', texto)
        self.assertIn("vars.PQD_AUTOMACAO_ATIVA == 'true'", texto)
        self.assertIn("vars.PQD_PUBLICACAO_CONFIRMADA == 'true'", texto)
        self.assertIn("environment: palavra-que-desperta-publicacao-meta", texto)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", texto)
        self.assertIn("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065", texto)
        self.assertIn("persist-credentials: false", texto)
        self.assertIn("github.event.repository.visibility", texto)
        self.assertNotIn("git push", texto)
        self.assertNotIn("DATA_PUBLICACAO", texto)

    def test_dependencias_exatamente_fixadas(self) -> None:
        linhas = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(linhas), 6)
        for pacote in ("requests==2.34.2", "certifi==2026.6.17", "charset-normalizer==3.4.9", "idna==3.18", "urllib3==2.7.0", "tzdata==2026.3"):
            self.assertTrue(any(linha.startswith(pacote + " --hash=sha256:") for linha in linhas))
        workflow = (ROOT / ".github" / "workflows" / "publicar-meta.yml").read_text(encoding="utf-8")
        self.assertIn("--require-hashes --only-binary=:all:", workflow)


if __name__ == "__main__":
    unittest.main()
