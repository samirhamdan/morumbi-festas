"""Sprint 2.1 — operação x canal x fonte e classificação pelo estado real."""

import sqlite3
from datetime import date, timedelta

import pytest

from sistema import dados, formato

HOJE = date.fromisoformat(formato.agora()[:10])
FUTURO = (HOJE + timedelta(days=23)).isoformat()
ANTES_DO_CORTE = "2025-03-10"


def _cliente(nome="Paula dos Santos Antunes Rocha", whatsapp="", email=""):
    with dados.conectar() as conn:
        return conn.execute(
            "INSERT INTO clientes (nome, whatsapp, email, criado_em) VALUES (?,?,?,?)",
            (nome, whatsapp, email, formato.agora())).lastrowid


def _importado(cliente_id, data_evento, origem_id=594, status="aprovado",
               canal="Google", valor=0, descricao="Kit Safari",
               origem="Formulario Festas"):
    with dados.conectar() as conn:
        return conn.execute(
            "INSERT INTO eventos_historico (cliente_id, origem_id, origem,"
            " data_evento, descricao, observacoes, canal, valor, status_origem,"
            " criado_em) VALUES (?,?,?,?,?,'Pgto: Pix',?,?,?,"
            " '2026-06-02 09:00:00')",
            (cliente_id, origem_id, origem, data_evento, descricao, canal,
             valor, status)).lastrowid


def _pedido(cliente_id, evento, sc="confirmado", so="preparacao",
            devolucao=None, historico=0, itens=True):
    agora = formato.agora()
    with dados.conectar() as conn:
        pid = conn.execute(
            "INSERT INTO pedidos (cliente_id, data_evento, data_retirada,"
            " data_devolucao, status_comercial, status_operacional, historico,"
            " criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?,?)",
            (cliente_id, evento, evento, devolucao or evento, sc, so,
             historico, agora, agora)).lastrowid
        if itens:
            conn.execute(
                "INSERT INTO itens_pedido (pedido_id, tipo, descricao, quantidade,"
                " preco_unitario) VALUES (?, 'kit', 'Kit Safari', 1, 300)", (pid,))
    return pid


def _reclassificar():
    with dados.conectar() as conn:
        return dados.aplicar_reclassificacao(conn, usuario_id=1)


def _diagnostico():
    with dados.conectar() as conn:
        return dados.diagnostico_reclassificacao(conn)


def _promovido(hid):
    with dados.conectar() as conn:
        r = conn.execute("SELECT * FROM pedidos WHERE historico_id = ?",
                         (hid,)).fetchone()
    return dict(r) if r else None


def _aba(aba, **filtros):
    return {(r["tipo"], r["id"]) for r in
            dados.consultar_pedidos(aba=aba, por_pagina=50, **filtros)["registros"]}


def _contagens():
    with dados.conectar() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("pedidos", "clientes", "eventos_historico",
                          "itens_pedido", "audit_log")}


# --- Os 11 testes obrigatórios ---------------------------------------------

class TesteObrigatorios:
    def test_1_pedido_futuro_importado_fica_em_andamento(self, app, admin):
        cli = _cliente()
        hid = _importado(cli, FUTURO)
        # antes da rotina já não é histórico nem modo consulta
        assert ("historico", hid) in _aba("andamento")
        assert ("historico", hid) not in _aba("historico")
        d = admin.get(f"/pedido/historico/{hid}")
        assert "modo consulta" not in d.text and "Festa importada #594" in d.text

        _reclassificar()
        p = _promovido(hid)
        assert (p["status_comercial"], p["status_operacional"], p["historico"]) == (
            "confirmado", "preparacao", 0)
        assert p["data_evento"] == FUTURO and p["cliente_id"] == cli
        assert ("pedido", p["id"]) in _aba("andamento")
        assert _aba("historico") == set()
        # agenda: uma única vez, pelo próprio pedido
        assert [e["id"] for e in dados.eventos_agenda(FUTURO, FUTURO)] == [p["id"]]
        assert dados.eventos_historico(FUTURO, FUTURO) == []
        # esteira do Sprint 3: pedido atual entra, pela etapa inicial
        esteira = {e["chave"]: [x["id"] for x in e["pedidos"]]
                   for e in dados.esteira_pedidos()}
        assert any(p["id"] in ids for ids in esteira.values())
        # link antigo leva ao pedido; busca pelo número da planilha encontra
        r = admin.get(f"/pedido/historico/{hid}")
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/pedido/{p['id']}")
        assert _aba("todos", q="594") == {("pedido", p["id"])}

    def test_2_pedido_futuro_sem_valor_continua_sem_valor(self, app, admin):
        hid = _importado(_cliente(), FUTURO, valor=0)
        _reclassificar()
        p = _promovido(hid)
        assert p["valor_informado"] is None
        reg = dados.consultar_pedidos(aba="andamento")["registros"][0]
        assert reg["total"] is None
        d = admin.get(f"/pedido/{p['id']}")
        assert "Valor não informado" in d.text
        assert dados.faturamento_periodo(FUTURO, FUTURO)["quantidade"] == 0

    def test_3_pedido_futuro_sem_itens_nao_ganha_itens(self, app, admin):
        hid = _importado(_cliente(), FUTURO, descricao="Kit Safari")
        _reclassificar()
        p = _promovido(hid)
        assert dados.buscar_pedido_festas(p["id"])["itens"] == []
        assert ("pedido", p["id"]) in _aba("andamento")
        d = admin.get(f"/pedido/{p['id']}")
        assert "Nenhum item cadastrado" in d.text
        # a descrição da planilha fica como observação, não como item
        assert "Pedido na planilha: Kit Safari" in p["observacoes"]
        assert "Editar pedido" in d.text  # disponível para complementação

    def test_4_pedido_futuro_cancelado_fica_cancelado(self, app, admin):
        cli = _cliente()
        pid = _pedido(cli, FUTURO, sc="cancelado", so="cancelado")
        hid = _importado(cli, FUTURO, status="cancelado", origem_id=595)
        _reclassificar()
        assert _promovido(hid) is None
        assert {("pedido", pid), ("historico", hid)} <= _aba("cancelados")
        assert _aba("andamento") == set()
        assert dados.eventos_agenda(FUTURO, FUTURO) == []
        assert sum(e["total"] for e in dados.esteira_pedidos()) == 0

    def test_5_pedido_passado_finalizado_fica_finalizado_historico(self, app, admin):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE, sc="finalizado", so="finalizado", historico=1)
        hid = _importado(cli, ANTES_DO_CORTE, status="entregue", origem_id=10)
        _reclassificar()
        assert _promovido(hid) is None
        for aba in ("finalizados", "historico"):
            assert {("pedido", pid), ("historico", hid)} <= _aba(aba)
        assert _aba("andamento") == set()
        d = admin.get(f"/pedido/historico/{hid}")
        assert "modo consulta" in d.text

    def test_6_pedido_passado_com_devolucao_pendente_continua_em_andamento(self, app):
        cli = _cliente()
        evento = (HOJE - timedelta(days=4)).isoformat()
        devolucao = (HOJE - timedelta(days=2)).isoformat()
        pid = _pedido(cli, evento, so="entregue", devolucao=devolucao)
        conferencia = _pedido(cli, evento, so="recolhido")
        _reclassificar()
        with dados.conectar() as conn:
            linhas = {r["id"]: tuple(r) for r in conn.execute(
                "SELECT id, status_comercial, status_operacional, historico"
                " FROM pedidos")}
        assert linhas[pid][1:] == ("confirmado", "entregue", 0)
        assert linhas[conferencia][1:] == ("confirmado", "recolhido", 0)
        assert {("pedido", pid), ("pedido", conferencia)} <= _aba("andamento")
        assert any(a["tipo"] == "devolucao_atrasada" for a in dados.alertas_dashboard())
        assert _diagnostico()["pedidos"]["passados_com_operacao_pendente"] == 2

    def test_7_historico_real_fica_em_modo_consulta(self, app, admin):
        hid = _importado(_cliente(), ANTES_DO_CORTE, status="aprovado", origem_id=12)
        _reclassificar()
        assert _promovido(hid) is None
        assert ("historico", hid) in _aba("historico")
        assert ("historico", hid) not in _aba("andamento")
        d = admin.get(f"/pedido/historico/{hid}")
        assert "modo consulta" in d.text and "Converter em pedido atual" not in d.text

    def test_8_formulario_festas_vira_morumbi_festas_e_fonte_e_preservada(self, app, admin):
        cli = _cliente()
        hid = _importado(cli, FUTURO, canal="Pesquisa no Google")
        with dados.conectar() as conn:
            antes = dict(conn.execute("SELECT * FROM eventos_historico WHERE id = ?",
                                      (hid,)).fetchone())
        _reclassificar()
        with dados.conectar() as conn:
            depois = dict(conn.execute("SELECT * FROM eventos_historico WHERE id = ?",
                                       (hid,)).fetchone())
        assert antes == depois  # registro importado intacto (fonte, número, canal)
        p = _promovido(hid)
        assert (p["fonte"], p["fonte_id"]) == ("Formulario Festas", 594)
        reg = dados.consultar_pedidos()["registros"][0]
        assert (reg["origem"], reg["fonte"]) == ("Morumbi Festas", "Formulario Festas")
        assert dados.origens_pedidos_unificados() == ["Morumbi Festas"]
        lista = admin.get("/pedidos")
        assert "Formulario Festas" not in lista.text and "Formulário Festas" in lista.text
        d = admin.get(f"/pedido/{p['id']}")
        assert "<dt>Origem</dt><dd>Morumbi Festas</dd>" in d.text
        assert "Fonte do registro</dt><dd>Formulário Festas #594" in d.text
        assert "Canal na planilha</dt><dd>Pesquisa no Google" in d.text

    def test_9_canal_google(self, app, admin):
        cli = _cliente()
        hid = _importado(cli, FUTURO, canal="google")
        outro = _importado(cli, FUTURO, canal="Instagram", origem_id=600)
        _reclassificar()
        p = _promovido(hid)
        assert p["canal"] == "Google"
        assert _aba("todos", canal="Google") == {("pedido", p["id"])}
        assert _aba("todos", canal="Instagram") == {("pedido", _promovido(outro)["id"])}
        d = admin.get(f"/pedido/{p['id']}")
        assert "<dt>Canal</dt><dd>Google</dd>" in d.text
        assert "<dt>Origem</dt><dd>Morumbi Festas</dd>" in d.text

    def test_10_cliente_com_varios_pedidos_continua_um_cliente(self, app, admin):
        cli = _cliente()
        atual = _pedido(cli, FUTURO)
        passado = _importado(cli, ANTES_DO_CORTE, status="entregue", origem_id=1)
        futuro = _importado(cli, FUTURO, origem_id=2)
        n_clientes = _contagens()["clientes"]
        _reclassificar()
        assert _contagens()["clientes"] == n_clientes
        ids = {r["cliente_id"] for r in dados.consultar_pedidos(por_pagina=50)["registros"]}
        assert ids == {cli}
        assert _promovido(futuro)["cliente_id"] == cli
        assert [p["id"] for p in dados.pedidos_cliente(cli)] != []
        assert {atual, _promovido(futuro)["id"]} == {p["id"] for p in dados.pedidos_cliente(cli)}
        assert [h["id"] for h in dados.eventos_historico_cliente(cli)] == [passado]

    def test_11_reexecutar_nao_duplica_nem_altera(self, app):
        cli = _cliente()
        _importado(cli, FUTURO)
        _importado(cli, FUTURO, origem_id=700, valor=450)
        _pedido(cli, FUTURO, historico=1)  # marcação inconsistente
        primeira = _reclassificar()
        assert len(primeira["promovidos"]) == 2 and len(primeira["desmarcados"]) == 1
        antes = _contagens()
        segunda = _reclassificar()
        assert segunda == {"promovidos": [], "desmarcados": []}
        assert _contagens() == antes
        with dados.conectar() as conn:
            hid = conn.execute("SELECT historico_id FROM pedidos"
                               " WHERE historico_id IS NOT NULL").fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO pedidos (cliente_id, historico_id)"
                             " VALUES (?, ?)", (cli, hid))


# --- Regras complementares ---------------------------------------------------

class TesteReclassificacao:
    def test_valor_da_importacao_e_preservado_sem_duplicar_faturamento(self, app, admin):
        hid = _importado(_cliente(), FUTURO, valor=450)
        _reclassificar()
        p = _promovido(hid)
        assert p["valor_informado"] == 450
        assert dados.consultar_pedidos()["registros"][0]["total"] == 450
        with dados.conectar() as conn:
            conn.execute("UPDATE pedidos SET status_comercial = 'finalizado',"
                         " status_operacional = 'finalizado' WHERE id = ?", (p["id"],))
        fat = dados.faturamento_periodo(FUTURO, FUTURO)
        assert (fat["quantidade"], fat["total"]) == (1, 450)
        assert admin.get(f"/pedido/{p['id']}").text.count("valor informado na importação") == 1

    def test_sem_data_ou_sem_cliente_vai_para_analise_manual(self, app, admin):
        cli = _cliente()
        sem_data = _importado(cli, None, origem_id=801)
        sem_cliente = _importado(None, FUTURO, origem_id=802)
        _reclassificar()
        assert _promovido(sem_data) is None and _promovido(sem_cliente) is None
        diag = _diagnostico()
        motivos = {h["id"]: h["motivos"] for h in diag["importados"]["sem_evidencia"]}
        assert motivos == {sem_data: ["sem data do evento"],
                           sem_cliente: ["sem cliente vinculado"]}
        assert diag["resumo"]["analise_manual"] == 2
        d = admin.get(f"/pedido/historico/{sem_data}")
        assert "Precisa de análise: sem data do evento" in d.text
        assert admin.post(f"/pedido/historico/{sem_data}/converter").status_code == 302
        assert _promovido(sem_data) is None

    def test_admin_converte_um_importado_pela_tela(self, app, admin):
        hid = _importado(_cliente(), FUTURO)
        assert "Converter em pedido atual" in admin.get(f"/pedido/historico/{hid}").text
        r = admin.post(f"/pedido/historico/{hid}/converter")
        p = _promovido(hid)
        assert r.headers["Location"].endswith(f"/pedido/{p['id']}")
        admin.post(f"/pedido/historico/{hid}/converter")  # idempotente
        assert _contagens()["pedidos"] == 1
        tempo = [e["titulo"] for e in dados.buscar_pedido_detalhe(p["id"])["linha_do_tempo"]]
        assert tempo[:2] == ["Registro importado", "Reclassificado como pedido atual"]

    def test_so_admin_converte(self, app, admin, client):
        from tests.test_pedidos_modulo import _entrar_como
        hid = _importado(_cliente(), FUTURO)
        comercial = _entrar_como(admin, client, "comercial")
        assert comercial.post(f"/pedido/historico/{hid}/converter").status_code in (302, 403)
        assert "Converter em pedido atual" not in comercial.get(f"/pedido/historico/{hid}").text
        assert _promovido(hid) is None

    def test_morumbi_3d_nunca_e_promovido(self, app):
        hid = _importado(_cliente(), FUTURO, origem="Morumbi 3D")
        _reclassificar()
        assert _promovido(hid) is None
        assert _aba("todos") == set()

    def test_marca_de_historico_inconsistente_e_corrigida(self, app):
        cli = _cliente()
        reaberto = _pedido(cli, ANTES_DO_CORTE, sc="confirmado", historico=1)
        futuro_final = _pedido(cli, FUTURO, sc="finalizado", so="finalizado", historico=1)
        legado = _pedido(cli, ANTES_DO_CORTE, sc="finalizado", so="finalizado", historico=1)
        r = _reclassificar()
        assert sorted(r["desmarcados"]) == sorted([reaberto, futuro_final])
        assert ("pedido", reaberto) in _aba("andamento")
        assert ("pedido", futuro_final) in _aba("finalizados")
        assert _aba("historico") == {("pedido", legado)}

    def test_admin_reabre_historico_e_a_marca_sai(self, app):
        cli = _cliente()
        pid = _pedido(cli, ANTES_DO_CORTE, sc="finalizado", so="finalizado", historico=1)
        p = dados.buscar_pedido_festas(pid)
        dados.salvar_pedido_festas(
            {"cliente_id": cli, "data_evento": p["data_evento"],
             "status_comercial": "confirmado", "status_operacional": "entregue"},
            [{"tipo": "kit", "descricao": "Kit Safari", "quantidade": 1,
              "preco_unitario": 300}], pid, usuario_id=1)
        assert dados.buscar_pedido_festas(pid)["historico"] == 0
        assert ("pedido", pid) in _aba("andamento")

    def test_canal_no_formulario_do_pedido(self, app, admin):
        cli = _cliente()
        pid = _pedido(cli, FUTURO)
        base = {"cliente_id": cli, "data_evento": FUTURO, "canal": "Instagram"}
        dados.salvar_pedido_festas(dict(base), [{"tipo": "kit", "descricao": "Kit",
                                    "quantidade": 1, "preco_unitario": 10}], pid)
        assert dados.buscar_pedido_festas(pid)["canal"] == "Instagram"
        with pytest.raises(dados.ErroDeCampo):
            dados.salvar_pedido_festas(dict(base, canal="Formulario Festas"), [], pid)
        form = admin.get(f"/pedido/{pid}/editar").text
        assert 'name="canal"' in form and '<option value="Instagram" selected>' in form

    def test_canal_canonico(self):
        casos = {"": "", "google": "Google", "Pesquisa no Google": "Google",
                 "insta": "Instagram", "Zap": "WhatsApp", "Indicação de amiga": "Indicação",
                 "Formulário": "Formulário", "site": "Site", "Panfleto": "Outro"}
        assert {k: dados.canal_canonico(k) for k in casos} == casos


class TesteRelatorio:
    def test_diagnostico_resume_os_numeros_do_relatorio(self, app):
        cli = _cliente()
        _importado(cli, FUTURO)                                            # a promover
        _importado(cli, ANTES_DO_CORTE, status="entregue", origem_id=2)    # histórico
        _importado(cli, FUTURO, status="cancelado", origem_id=3)           # cancelado
        _importado(cli, FUTURO, canal="", origem_id=4)                     # sem canal
        _pedido(cli, FUTURO)
        r = _diagnostico()["resumo"]
        assert r["analisados"] == 5
        assert r["historicos"] == 1 and r["cancelados"] == 1
        assert r["futuros"] == 3 and r["a_reclassificar"] == 2
        assert r["formulario_festas"] == 4 and r["canal_identificado"] == 3
        _reclassificar()
        depois = _diagnostico()["resumo"]
        assert depois["reclassificados"] == 2 and depois["a_reclassificar"] == 0
        assert depois["analisados"] == 5 and depois["formulario_festas"] == 4

    def test_clientes_duplicados_sao_relatados_e_nunca_mesclados(self, app):
        a = _cliente("Paula Rocha", whatsapp="(11) 99999-1111")
        b = _cliente("Paula  Rocha")
        c = _cliente("Outra Pessoa", whatsapp="5511999991111")
        _cliente("Paula")  # só o primeiro nome não é indício
        antes = _contagens()["clientes"]
        with dados.conectar() as conn:
            grupos = dados.possiveis_clientes_duplicados(conn)
        assert [[m["id"] for m in g["clientes"]] for g in grupos] == [[a, b, c]]
        assert grupos[0]["motivos"] == ["mesmo nome", "mesmo telefone"]
        assert _contagens()["clientes"] == antes

    def test_vinculo_por_nome_ambiguo_vai_para_analise(self, app):
        a = _cliente("Paula Rocha")
        _cliente("Paula Rocha")
        hid = _importado(a, ANTES_DO_CORTE, status="entregue", origem_id=9)
        diag = _diagnostico()
        assert [h["id"] for h in diag["vinculos_ambiguos"]] == [hid]
        assert diag["resumo"]["analise_manual"] == 1

    def test_finalizado_pelo_sprint1_com_operacao_em_curso_e_listado(self, app):
        cli = _cliente()
        pid = _pedido(cli, "2026-09-20", so="entregue", devolucao="2026-09-22")
        with dados.conectar() as conn:
            dados.aplicar_normalizacao(conn)
        diag = _diagnostico()
        assert [p["id"] for p in diag["finalizados_com_operacao_em_curso"]] == [pid]
        assert diag["finalizados_com_operacao_em_curso"][0]["status_antes"] == "entregue"
        # a rotina do 2.1 não desfaz a decisão do Sprint 1 sem análise
        _reclassificar()
        assert dados.buscar_pedido_festas(pid)["status_comercial"] == "finalizado"
