"""Sprint 1 — normalização dos pedidos históricos e faturamento."""

import json
from datetime import date

import pytest

from sistema import dados, formato

ANTES_DO_CORTE = "2026-09-05"
DEPOIS_DO_CORTE = "2026-10-10"


def _cliente(nome="Thaís Oshita"):
    with dados.conectar() as conn:
        return conn.execute(
            "INSERT INTO clientes (nome, criado_em) VALUES (?, ?)",
            (nome, formato.agora())).lastrowid


def _pedido(cliente_id, data_evento, sc="confirmado", so="preparacao",
            valor=150.0, produto_id=None, retirada=None, devolucao=None):
    agora = formato.agora()
    with dados.conectar() as conn:
        pid = conn.execute(
            "INSERT INTO pedidos (cliente_id, data_evento, data_retirada,"
            " data_devolucao, status_comercial, status_operacional,"
            " criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?)",
            (cliente_id, data_evento, retirada or data_evento,
             devolucao or data_evento, sc, so, agora, agora)).lastrowid
        if valor:
            conn.execute(
                "INSERT INTO itens_pedido (pedido_id, tipo, item_id, descricao,"
                " quantidade, preco_unitario) VALUES (?, 'produto', ?, 'Kit', 1, ?)",
                (pid, produto_id, valor))
    return pid


def _importado(cliente_id, data_evento, origem="Formulario Festas",
               status="aprovado", valor=0, origem_id=None,
               criado_em="2026-09-19 10:00:00"):
    with dados.conectar() as conn:
        n = conn.execute("SELECT COUNT(*) FROM eventos_historico").fetchone()[0]
        return conn.execute(
            "INSERT INTO eventos_historico (cliente_id, origem_id, origem,"
            " data_evento, descricao, valor, status_origem, criado_em)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (cliente_id, origem_id or 1000 + n, origem, data_evento, "Festa",
             valor, status, criado_em)).lastrowid


def _normalizar():
    with dados.conectar() as conn:
        return dados.aplicar_normalizacao(conn)


def _status(pid):
    with dados.conectar() as conn:
        return dict(conn.execute(
            "SELECT status_comercial, status_operacional, historico"
            " FROM pedidos WHERE id = ?", (pid,)).fetchone())


def _unificado(tipo, id_):
    return next(r for r in dados.listar_pedidos_unificados()
                if r["tipo"] == tipo and r["id"] == id_)


class TesteCenariosObrigatorios:
    def test_1_pedido_historico_fica_finalizado_e_fora_da_esteira(self, app):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE)
        imp = _importado(cli, "2025-01-25")
        _normalizar()

        assert _status(pid) == {"status_comercial": "finalizado",
                                "status_operacional": "finalizado",
                                "historico": 1}
        assert pid not in [p["id"] for p in dados.listar_pedidos_operacional()]
        assert all(pid not in [p["id"] for p in e["pedidos"]]
                   for e in dados.esteira_pedidos())
        assert sum(e["total"] for e in dados.esteira_pedidos()) == 0
        assert dados.indicadores_dashboard()["pedidos_ativos"] == 0
        assert dados.alertas_dashboard() == []
        r = _unificado("historico", imp)
        assert (r["status_comercial"], r["historico"]) == ("finalizado", True)
        assert r["origem"] == "Formulario Festas"

    def test_2_historico_com_valor_entra_no_faturamento(self, app):
        cli = _cliente()
        _pedido(cli, ANTES_DO_CORTE, valor=200)
        _importado(cli, "2026-09-12", valor=240)
        _normalizar()
        fat = dados.faturamento_periodo("2026-09-01", "2026-09-30")
        assert fat["total"] == 440
        assert fat["quantidade"] == 2

    def test_3_historico_sem_valor_nao_soma_nem_gera_erro(self, app, admin):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE, valor=None)
        _importado(cli, "2026-09-10", valor=0)
        _importado(cli, "2026-09-11", valor=None)
        _pedido(cli, "2026-09-12", valor=300)
        _normalizar()
        assert _status(pid)["historico"] == 1
        fat = dados.faturamento_periodo("2026-09-01", "2026-09-30")
        assert fat["total"] == 300
        assert fat["quantidade"] == 4
        assert fat["sem_valor"] == 3
        r = admin.get("/faturamento?periodo=personalizado&inicio=2026-09-01&fim=2026-09-30")
        assert r.status_code == 200
        assert "não informado" in r.text

    def test_4_pedido_atual_em_preparacao_continua_na_esteira(self, app):
        cli = _cliente()
        pid = _pedido(cli, DEPOIS_DO_CORTE)
        _normalizar()
        assert _status(pid) == {"status_comercial": "confirmado",
                                "status_operacional": "preparacao",
                                "historico": 0}
        assert pid in [p["id"] for p in dados.listar_pedidos_operacional()]
        confirmados = dados.esteira_pedidos()[0]
        assert pid in [p["id"] for p in confirmados["pedidos"]]
        assert dados.indicadores_dashboard()["pedidos_ativos"] == 1

    def test_5_cancelado_nao_fatura_nem_conta_como_festa(self, app):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE, sc="cancelado", so="cancelado")
        _importado(cli, "2026-09-06", status="cancelado", valor=500)
        _normalizar()
        dados.atualizar_todas_classificacoes()
        assert _status(pid)["status_comercial"] == "cancelado"
        assert _status(pid)["historico"] == 0
        assert dados.faturamento_periodo("2026-09-01", "2026-09-30")["total"] == 0
        with dados.conectar() as conn:
            total = conn.execute("SELECT total_festas FROM clientes WHERE id=?",
                                 (cli,)).fetchone()[0]
        assert total == 0

    def test_6_morumbi_3d_mantem_origem_e_fica_finalizado(self, app):
        cli = _cliente()
        imp = _importado(cli, "2024-06-15", origem="Morumbi 3D",
                         status="entregue", valor=500)
        with dados.conectar() as conn:
            antes = dict(conn.execute("SELECT * FROM eventos_historico WHERE id=?",
                                      (imp,)).fetchone())
        _normalizar()
        with dados.conectar() as conn:
            depois = dict(conn.execute("SELECT * FROM eventos_historico WHERE id=?",
                                       (imp,)).fetchone())
        assert depois == antes
        r = _unificado("historico", imp)
        assert r["origem"] == "Morumbi 3D"
        assert r["status_comercial"] == "finalizado"
        assert r["historico"] is True

    def test_7_reexecutar_nao_duplica_nem_altera(self, app):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE)
        _pedido(cli, DEPOIS_DO_CORTE)
        _importado(cli, "2025-03-03")
        assert _normalizar()["alterados"] == 1
        with dados.conectar() as conn:
            antes = [dict(r) for r in conn.execute("SELECT * FROM pedidos ORDER BY id")]
            n_hist = conn.execute("SELECT COUNT(*) FROM eventos_historico").fetchone()[0]
        assert _normalizar() == {"alterados": 0, "ids": []}
        with dados.conectar() as conn:
            depois = [dict(r) for r in conn.execute("SELECT * FROM pedidos ORDER BY id")]
            assert conn.execute("SELECT COUNT(*) FROM eventos_historico"
                                ).fetchone()[0] == n_hist
            auditorias = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE tipo='normalizacao_historico'"
            ).fetchone()[0]
        assert depois == antes
        assert auditorias == 1
        assert _status(pid)["historico"] == 1

    def test_8_faturamento_do_periodo_e_a_soma_real(self, app):
        cli = _cliente()
        _pedido(cli, "2026-09-05", valor=200)
        _importado(cli, "2026-09-12", valor=240)
        _pedido(cli, "2026-09-19", sc="devolvido", so="conferido", valor=300)
        _pedido(cli, "2026-09-20", valor=999)
        _pedido(cli, "2026-09-21", sc="cancelado", so="cancelado", valor=999)
        _pedido(cli, "2026-08-31", sc="devolvido", so="conferido", valor=999)
        _normalizar()
        fat = dados.faturamento_periodo("2026-09-01", "2026-09-30")
        assert fat["total"] == 740
        assert fat["quantidade"] == 3
        assert dados.faturamento_mensal(2026, 9)["total"] == 740


class TesteRegras:
    def test_origem_de_todos_os_importados_e_preservada(self, app):
        cli = _cliente()
        for origem in ("Formulario Festas", "Morumbi 3D", "Morumbi Festas"):
            _importado(cli, "2025-05-05", origem=origem, status="entregue")
        _normalizar()
        with dados.conectar() as conn:
            origens = sorted(r[0] for r in conn.execute(
                "SELECT origem FROM eventos_historico"))
        assert origens == ["Formulario Festas", "Morumbi 3D", "Morumbi Festas"]

    def test_3d_usa_status_proprio_e_planilha_usa_data(self, app):
        assert dados.situacao_historico("Morumbi 3D", "aprovado", "2024-11-01") == "pendente"
        assert dados.situacao_historico("Morumbi 3D", "entregue", None) == "finalizado"
        assert dados.situacao_historico("Formulario Festas", "aprovado", "2025-01-25") == "finalizado"
        assert dados.situacao_historico("Formulario Festas", "aprovado", DEPOIS_DO_CORTE) == "pendente"
        assert dados.situacao_historico("Formulario Festas", "aprovado", None) == "pendente"
        assert dados.situacao_historico("Morumbi 3D", "Cancelado", "2024-01-01") == "cancelado"

    def test_faturamento_usa_data_do_evento_e_nao_a_importacao(self, app):
        cli = _cliente()
        _importado(cli, "2025-03-10", status="entregue", valor=180,
                   criado_em="2026-09-19 10:00:00")
        assert dados.faturamento_periodo("2025-03-01", "2025-03-31")["total"] == 180
        assert dados.faturamento_periodo("2026-09-01", "2026-09-30")["total"] == 0

    def test_historico_nao_bloqueia_estoque(self, app):
        prod = dados.salvar_produto({"nome": "Mesa", "status": "disponivel",
                                     "quantidade_total": 2})
        cli = _cliente()
        _pedido(cli, ANTES_DO_CORTE, produto_id=prod, retirada="2026-09-04",
                devolucao="2026-09-06")
        assert dados.disponibilidade(prod, "2026-09-05", "2026-09-05") == 1
        _normalizar()
        assert dados.disponibilidade(prod, "2026-09-05", "2026-09-05") == 2

    def test_historico_continua_no_cliente_e_na_classificacao(self, app):
        cli = _cliente()
        _pedido(cli, ANTES_DO_CORTE)
        _importado(cli, "2025-01-25")
        _importado(cli, "2024-02-02", origem="Morumbi 3D", status="aprovado")
        _normalizar()
        dados.atualizar_todas_classificacoes()
        assert len(dados.pedidos_cliente(cli)) == 1
        assert len(dados.eventos_historico_cliente(cli)) == 2
        with dados.conectar() as conn:
            c = dict(conn.execute("SELECT total_festas, ultima_festa FROM clientes"
                                  " WHERE id=?", (cli,)).fetchone())
        assert c == {"total_festas": 2, "ultima_festa": ANTES_DO_CORTE}

    def test_auditoria_guarda_estado_anterior(self, app):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE, sc="entregue", so="entregue")
        _normalizar()
        with dados.conectar() as conn:
            reg = conn.execute("SELECT dados FROM audit_log"
                               " WHERE tipo='normalizacao_historico'").fetchone()
        alterado = json.loads(reg["dados"])["alterados"][0]
        assert alterado == {"id": pid, "antes": {"status_comercial": "entregue",
                                                  "status_operacional": "entregue",
                                                  "historico": 0}}

    def test_pedido_sem_data_nao_e_alterado(self, app):
        cli = _cliente()
        with dados.conectar() as conn:
            pid = conn.execute(
                "INSERT INTO pedidos (cliente_id, criado_em, atualizado_em)"
                " VALUES (?, '2025-01-01', '2025-01-01')", (cli,)).lastrowid
            diag = dados.diagnostico_normalizacao(conn)
        assert diag["pedidos"]["sem_data"] == 1
        _normalizar()
        assert _status(pid)["status_comercial"] == "confirmado"

    def test_diagnostico_resume_o_que_sera_feito(self, app):
        cli = _cliente()
        _pedido(cli, ANTES_DO_CORTE)
        _pedido(cli, "2026-09-01", sc="finalizado", so="finalizado")
        _pedido(cli, DEPOIS_DO_CORTE)
        _pedido(cli, ANTES_DO_CORTE, sc="cancelado", so="cancelado")
        _importado(cli, "2024-02-02", origem="Morumbi 3D", status="entregue", valor=90)
        _importado(cli, DEPOIS_DO_CORTE)
        with dados.conectar() as conn:
            d = dados.diagnostico_normalizacao(conn)
        p = d["pedidos"]
        assert (p["total"], p["historicos"], p["a_alterar"]) == (4, 2, 2)
        assert p["ja_finalizados_sem_marca"] == 1
        assert (p["atuais_preservados"], p["cancelados"]) == (1, 1)
        assert d["historico"]["por_origem"]["Morumbi 3D"]["finalizado"] == 1
        assert len(d["historico"]["pendentes"]) == 1


class TestePeriodos:
    HOJE = date(2026, 9, 23)

    @pytest.mark.parametrize("chave,esperado", [
        ("este_mes", ("2026-09-01", "2026-09-30")),
        ("mes_anterior", ("2026-08-01", "2026-08-31")),
        ("este_ano", ("2026-01-01", "2026-12-31")),
        ("qualquer", ("2026-09-01", "2026-09-30")),
    ])
    def test_presets(self, chave, esperado):
        assert dados.intervalo_periodo(chave, self.HOJE) == esperado

    def test_mes_anterior_em_janeiro(self):
        assert dados.intervalo_periodo("mes_anterior", date(2026, 1, 10)) == (
            "2025-12-01", "2025-12-31")

    def test_personalizado(self):
        assert dados.intervalo_periodo("personalizado", self.HOJE,
                                       "2026-02-10", "2026-03-05") == (
            "2026-02-10", "2026-03-05")
        with pytest.raises(ValueError):
            dados.intervalo_periodo("personalizado", self.HOJE, "2026-03-05", "2026-02-10")
        with pytest.raises(ValueError):
            dados.intervalo_periodo("personalizado", self.HOJE, "x", "")

    def test_pagina_e_api(self, app, admin):
        cli = _cliente()
        _importado(cli, "2025-03-10", status="entregue", valor=180)
        r = admin.get("/faturamento?periodo=personalizado&inicio=2025-03-01&fim=2025-03-31")
        assert "180,00" in r.text and "Formulario Festas" in r.text
        corpo = admin.get("/api/faturamento?periodo=personalizado"
                          "&inicio=2025-01-01&fim=2025-12-31").get_json()
        assert corpo["total"] == 180 and corpo["periodo"] == "personalizado"
        r = admin.get("/faturamento?periodo=personalizado&inicio=2025-05-01&fim=2025-01-01")
        assert "A data inicial deve ser anterior" in r.text
        assert admin.get("/faturamento?ano=2025&mes=3").status_code == 200


class TesteFerramenta:
    def _ferramenta(self):
        import importlib.util
        from pathlib import Path
        caminho = Path(__file__).resolve().parent.parent / "ferramentas" / "normalizar_historico.py"
        spec = importlib.util.spec_from_file_location("normalizar_historico", caminho)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_simulacao_nao_altera_o_banco_real(self, app, capsys):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE)
        real = dados.CAMINHO_BD
        self._ferramenta().executar(real, 5, aplicar=False)
        saida = capsys.readouterr().out
        dados.CAMINHO_BD = real
        assert _status(pid)["status_comercial"] == "confirmado"
        assert "Pedidos que serão alterados ........ 1" in saida
        assert "Idempotência: segunda execução alterou 0 pedidos" in saida
        assert "históricos na esteira" in saida

    def test_aplicar_faz_backup_antes(self, app, capsys, tmp_path):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE)
        real = dados.CAMINHO_BD
        self._ferramenta().executar(real, 5, aplicar=True)
        dados.CAMINHO_BD = real
        backups = list(tmp_path.glob("*.backup-antes-normalizacao-*"))
        assert len(backups) == 1
        import sqlite3
        b = sqlite3.connect(backups[0])
        assert b.execute("SELECT status_comercial FROM pedidos WHERE id=?",
                         (pid,)).fetchone()[0] == "confirmado"
        b.close()
        assert _status(pid)["status_comercial"] == "finalizado"


class TesteValorManual:
    @pytest.mark.parametrize("texto,esperado", [
        ("150", 150.0), ("150,00", 150.0), ("1.250,50", 1250.5),
        ("R$ 1.250,50", 1250.5), ("99.9", 99.9), ("", None), ("  ", None),
    ])
    def test_ler_dinheiro(self, texto, esperado):
        assert formato.ler_dinheiro(texto) == esperado

    @pytest.mark.parametrize("texto", ["abc", "-10", "1,2,3"])
    def test_ler_dinheiro_invalido(self, texto):
        with pytest.raises(ValueError):
            formato.ler_dinheiro(texto)

    def test_valor_informado_entra_no_faturamento_e_e_auditado(self, app, admin):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", status="entregue", valor=0, origem_id=77)
        assert dados.faturamento_mensal(2026, 8)["sem_valor"] == 1
        r = admin.post(f"/faturamento/historico/{imp}",
                       data={"valor": "1.250,50",
                             "voltar": "/faturamento?periodo=personalizado&inicio=2026-08-01&fim=2026-08-31"})
        assert r.status_code == 302
        assert r.headers["Location"].startswith("/faturamento?periodo=personalizado")
        fat = dados.faturamento_mensal(2026, 8)
        assert (fat["total"], fat["sem_valor"]) == (1250.5, 0)
        with dados.conectar() as conn:
            h = dict(conn.execute("SELECT origem, origem_id, data_evento, status_origem"
                                  " FROM eventos_historico WHERE id=?", (imp,)).fetchone())
            aud = conn.execute("SELECT dados FROM audit_log WHERE tipo='valor_historico'"
                               ).fetchone()
        assert h == {"origem": "Formulario Festas", "origem_id": 77,
                     "data_evento": "2026-08-15", "status_origem": "entregue"}
        assert json.loads(aud["dados"])["antes"] is None
        assert json.loads(aud["dados"])["depois"] == 1250.5

    def test_limpar_valor_volta_a_sem_valor(self, app):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", status="entregue", valor=100)
        dados.salvar_valor_historico(imp, None)
        assert dados.faturamento_mensal(2026, 8)["sem_valor"] == 1
        assert dados.salvar_valor_historico(imp, None)["alterado"] is False

    def test_morumbi_3d_nao_e_editavel(self, app, admin):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", origem="Morumbi 3D", status="entregue", valor=15)
        r = admin.post(f"/faturamento/historico/{imp}", data={"valor": "999"},
                       follow_redirects=True)
        assert "não são editados aqui" in r.text
        assert dados.faturamento_mensal(2026, 8)["total"] == 15
        pagina = admin.get("/faturamento?periodo=personalizado&inicio=2026-08-01&fim=2026-08-31")
        assert f"/faturamento/historico/{imp}" not in pagina.text

    def test_valor_invalido_mostra_erro(self, app, admin):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", status="entregue")
        r = admin.post(f"/faturamento/historico/{imp}", data={"valor": "abc"},
                       follow_redirects=True)
        assert "Valor inválido" in r.text

    def test_nao_redireciona_para_fora(self, app, admin):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", status="entregue")
        r = admin.post(f"/faturamento/historico/{imp}",
                       data={"valor": "10", "voltar": "https://exemplo.com"})
        assert r.headers["Location"] == "/faturamento"

    def test_operacional_nao_edita(self, app, admin, client):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", status="entregue")
        admin.post("/usuario", data={"nome": "Op", "login": "op", "senha": "op12345",
                                     "perfil": "operacional", "ativo": "1"})
        admin.get("/sair")
        client.post("/entrar", data={"login": "op", "senha": "op12345"})
        assert client.post(f"/faturamento/historico/{imp}",
                           data={"valor": "10"}).status_code == 403

    def test_filtro_so_sem_valor(self, app, admin):
        cli = _cliente()
        _importado(cli, "2026-08-10", status="entregue", valor=0, origem_id=501)
        _importado(cli, "2026-08-11", status="entregue", valor=80, origem_id=502)
        base = "/faturamento?periodo=personalizado&inicio=2026-08-01&fim=2026-08-31"
        assert "Mostrar só esses para preencher" in admin.get(base).text
        r = admin.get(base + "&sem_valor=1")
        assert "#501" in r.text and "#502" not in r.text
        assert "80,00" in r.text


class TesteDataManual:
    def test_data_futura_e_avisada_e_corrigida(self, app, admin):
        cli = _cliente("Thaís Oshita")
        imp = _importado(cli, "2035-01-25", status="entregue", valor=0, origem_id=351)
        dados.atualizar_todas_classificacoes()
        r = admin.get("/faturamento")
        assert "com data do evento no futuro" in r.text
        assert "Formulario Festas #351" in r.text
        assert "inicio=2035-01-25" in r.text

        r = admin.post(f"/faturamento/historico/{imp}",
                       data={"valor": "150,00", "data_evento": "2025-01-25"},
                       follow_redirects=True)
        assert "Alterações salvas." in r.text
        assert "com data do evento no futuro" not in r.text
        fat = dados.faturamento_mensal(2025, 1)
        assert (fat["total"], fat["quantidade"]) == (150.0, 1)
        assert dados.faturamento_periodo("2035-01-01", "2035-12-31")["quantidade"] == 0
        with dados.conectar() as conn:
            aud = {row["tipo"]: json.loads(row["dados"]) for row in conn.execute(
                "SELECT tipo, dados FROM audit_log"
                " WHERE tipo IN ('valor_historico','data_historico')")}
            ultima = conn.execute("SELECT ultima_festa FROM clientes WHERE id=?",
                                  (cli,)).fetchone()[0]
            origem = conn.execute("SELECT origem, origem_id FROM eventos_historico"
                                  " WHERE id=?", (imp,)).fetchone()
        assert (aud["data_historico"]["antes"], aud["data_historico"]["depois"]) == (
            "2035-01-25", "2025-01-25")
        assert aud["valor_historico"]["depois"] == 150.0
        assert ultima == "2025-01-25"
        assert tuple(origem) == ("Formulario Festas", 351)

    def test_so_valor_mantem_a_data(self, app):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", status="entregue")
        dados.salvar_historico_manual(imp, valor=90)
        with dados.conectar() as conn:
            assert conn.execute("SELECT data_evento FROM eventos_historico WHERE id=?",
                                (imp,)).fetchone()[0] == "2026-08-15"

    def test_data_invalida(self, app, admin):
        cli = _cliente()
        imp = _importado(cli, "2026-08-15", status="entregue")
        r = admin.post(f"/faturamento/historico/{imp}",
                       data={"valor": "", "data_evento": "31/02/2026"},
                       follow_redirects=True)
        assert "Data do evento inválida." in r.text

    def test_pagina_tem_campo_de_data_so_para_editaveis(self, app, admin):
        cli = _cliente()
        planilha = _importado(cli, "2026-08-10", status="entregue", origem_id=601)
        tres_d = _importado(cli, "2026-08-11", origem="Morumbi 3D", status="entregue",
                            valor=15, origem_id=602)
        r = admin.get("/faturamento?periodo=personalizado&inicio=2026-08-01&fim=2026-08-31")
        assert f'form="h-{planilha}"' in r.text
        assert f'form="h-{tres_d}"' not in r.text
