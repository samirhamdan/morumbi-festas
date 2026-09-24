"""Sprint 3 — Esteira de pedidos."""

import json
from datetime import date, timedelta

import pytest
from werkzeug.security import generate_password_hash

from sistema import dados, formato

HOJE = date.fromisoformat(formato.agora()[:10])
D = lambda n: (HOJE + timedelta(days=n)).isoformat()  # noqa: E731


def _cliente(nome="Camila Ferreira", whatsapp=""):
    return dados.salvar_cliente({"nome": nome, "whatsapp": whatsapp})


def _pedido(cli, evento=None, retirada=None, devolucao=None, itens=None, **extra):
    evento = evento or D(3)
    d = {"cliente_id": cli, "data_evento": evento,
         "data_retirada": retirada or evento, "data_devolucao": devolucao or evento}
    d.update(extra)
    return dados.salvar_pedido_festas(d, itens if itens is not None else [
        {"tipo": "kit", "descricao": "Kit Safari", "quantidade": 1, "preco_unitario": 300},
        {"tipo": "produto", "descricao": "Bandejas", "quantidade": 7, "preco_unitario": 10},
        {"tipo": "servico", "descricao": "Entrega", "quantidade": 1, "preco_unitario": 0}])


def _definir(pid, **campos):
    with dados.conectar() as conn:
        conn.execute("UPDATE pedidos SET " + ", ".join(f"{c} = ?" for c in campos)
                     + " WHERE id = ?", (*campos.values(), pid))


def _status(pid):
    p = dados.buscar_pedido_festas(pid)
    return p["status_comercial"], p["status_operacional"]


def _cartoes(dia=None, **filtros):
    q = dados.quadro_esteira(dia, filtros)
    return {c["id"]: (col["chave"], c) for col in q["colunas"] for c in col["cartoes"]}


def _mover(cliente, pid, destino, esperado=None):
    r = cliente.post(f"/operacao/pedido/{pid}/mover",
                     data={"destino": destino, "esperado": esperado or ""},
                     headers={"X-Requested-With": "fetch"})
    return r.status_code, (r.get_json() if r.is_json else None)


def _entrar(app, perfil):
    dados.salvar_usuario({"nome": perfil.title(), "login": f"e_{perfil}", "perfil": perfil},
                         senha_hash=generate_password_hash("x"))
    c = app.test_client()
    c.post("/entrar", data={"login": f"e_{perfil}", "senha": "x"})
    return c


class TesteFluxo:
    def test_fluxo_normal_ate_finalizado(self, app, admin):
        pid = _pedido(_cliente())
        caminho = [("separado", "separado"), ("entregue", "entregue"),
                   ("recolhido", "recolhido"), ("conferido", "conferencia"),
                   ("finalizado", "finalizado")]
        for destino, coluna in caminho:
            atual = _status(pid)[1]
            codigo, res = _mover(admin, pid, destino, esperado=atual)
            assert codigo == 200 and res["ok"], res
            assert _status(pid)[1] == destino
            assert _cartoes()[pid][0] == coluna
        assert _status(pid) == ("finalizado", "finalizado")
        tempo = [(e["titulo"], e["categoria"]) for e in
                 dados.buscar_pedido_detalhe(pid)["linha_do_tempo"]
                 if e["categoria"] != "Agenda" or "registrada" in e["titulo"]]
        assert ("Itens separados", "Operação") in tempo
        assert ("Retirada / entrega registrada", "Agenda") in tempo
        assert ("Devolução registrada", "Operação") in tempo
        assert ("Itens conferidos", "Operação") in tempo
        assert ("Pedido finalizado", "Comercial") in tempo

    def test_auditoria_registra_usuario_e_status(self, app, admin):
        pid = _pedido(_cliente())
        _mover(admin, pid, "separado", "preparacao")
        with dados.conectar() as conn:
            r = conn.execute("SELECT * FROM audit_log WHERE entidade = 'pedido'"
                             " AND entidade_id = ? ORDER BY id DESC", (pid,)).fetchone()
        d = json.loads(r["dados"])
        assert r["usuario_id"] == 1 and r["criado_em"]
        assert d["mudancas"]["status_operacional"] == ["preparacao", "separado"]
        assert d["titulo"] == "Itens separados"

    def test_montado_antigo_fica_em_separado_e_segue_para_entrega(self, app, admin):
        pid = _pedido(_cliente())
        _definir(pid, status_operacional="montado")
        assert _cartoes()[pid][0] == "separado"
        assert _cartoes()[pid][1]["acao"] == "Registrar retirada/entrega"
        assert _mover(admin, pid, "entregue", "montado")[0] == 200


class TesteTransicoesInvalidas:
    @pytest.mark.parametrize("preparar,destino", [
        ("cancelado", "preparacao"), ("historico", "preparacao"),
        ("finalizado", "separado"), ("preparacao", "conferido"),
        ("preparacao", "finalizado"), ("preparacao", "recolhido"),
        ("entregue", "separado"), ("separado", "finalizado"),
    ])
    def test_rejeitadas_no_backend(self, app, admin, preparar, destino):
        pid = _pedido(_cliente())
        if preparar == "cancelado":
            dados.cancelar_pedido(pid, "desistiu")
        elif preparar == "historico":
            _definir(pid, historico=1, status_comercial="finalizado",
                     status_operacional="finalizado")
        elif preparar == "finalizado":
            _definir(pid, status_comercial="finalizado", status_operacional="finalizado")
        else:
            _definir(pid, status_operacional=preparar)
        antes = _status(pid)
        codigo, res = _mover(admin, pid, destino)
        assert codigo == 409 and not res["ok"]
        assert _status(pid) == antes

    def test_etapa_esperada_desatualizada_e_recusada(self, app, admin):
        pid = _pedido(_cliente())
        dados.mover_etapa(pid, "separado")
        codigo, res = _mover(admin, pid, "separado", esperado="preparacao")
        assert codigo == 409 and "mudou de etapa" in res["mensagem"]
        assert _status(pid)[1] == "separado"

    def test_sem_javascript_o_formulario_volta_para_a_esteira(self, app, admin):
        pid = _pedido(_cliente())
        r = admin.post(f"/operacao/pedido/{pid}/mover",
                       data={"destino": "separado", "esperado": "preparacao",
                             "voltar": "/operacao?status=preparacao"})
        assert r.status_code == 302 and r.headers["Location"] == "/operacao?status=preparacao"
        r = admin.post(f"/operacao/pedido/{pid}/mover",
                       data={"destino": "x", "voltar": "https://externo"})
        assert r.headers["Location"] == "/operacao"


class TesteAtrasos:
    def test_no_prazo_atrasado_e_concluido_apos_atraso(self, app, admin):
        cli = _cliente()
        no_prazo = _pedido(cli, evento=D(2), retirada=D(1))
        atrasado = _pedido(cli, evento=D(1), retirada=D(-1))
        concluido = _pedido(cli, evento=D(-3), retirada=D(-4), devolucao=D(-2))
        for destino in ("separado", "entregue", "recolhido", "conferido", "finalizado"):
            dados.mover_etapa(concluido, destino)
        c = _cartoes()
        assert c[no_prazo][1]["situacao"] == "no_prazo"
        assert c[atrasado][1]["situacao"] == "atrasado"
        assert c[atrasado][1]["proxima"] == "Prioridade"
        assert c[concluido][1]["situacao"] == "concluido"
        k = dados.indicadores_esteira()
        assert (k["na_esteira"], k["atrasados"], k["no_prazo"]) == (2, 1, 1)

    def test_devolucao_e_conferencia_pendentes_atrasam(self, app, admin):
        cli = _cliente()
        com_cliente = _pedido(cli, evento=D(-3), retirada=D(-4), devolucao=D(-1))
        voltou = _pedido(cli, evento=D(-3), retirada=D(-4), devolucao=D(-1))
        _definir(com_cliente, status_operacional="entregue")
        _definir(voltou, status_operacional="recolhido")
        c = _cartoes()
        assert c[com_cliente][1]["situacao"] == "atrasado"
        assert c[voltou][1]["situacao"] == "atrasado"
        # a data de referência muda a leitura: dois dias antes, estavam no prazo
        c = _cartoes(D(-2))
        assert c[com_cliente][1]["situacao"] == "no_prazo"

    def test_dashboard_usa_a_mesma_regra(self, app, admin):
        cli = _cliente()
        p = _pedido(cli, evento=D(-3), retirada=D(-4), devolucao=D(-1))
        _definir(p, status_operacional="entregue")
        _pedido(cli)
        k = dados.indicadores_esteira()
        ind = dados.indicadores_dashboard()
        assert ind["pedidos_ativos"] == k["na_esteira"] == 2
        assert ind["pedidos_atrasados"] == k["atrasados"] == 1
        alertas = {a["tipo"]: a["quantidade"] for a in dados.alertas_dashboard()}
        assert alertas["devolucao_atrasada"] == 1
        assert sum(e["total"] for e in dados.esteira_pedidos()) == 2


class TesteHistorico:
    def test_historico_fica_fora_da_esteira_e_continua_no_resto(self, app, admin):
        cli = _cliente()
        hist = _pedido(cli, evento="2025-05-10")
        _definir(hist, historico=1, status_comercial="finalizado",
                 status_operacional="finalizado")
        importado = dados.salvar_evento_historico({
            "cliente_id": cli, "origem_id": 7, "origem": "Formulario Festas",
            "data_evento": "2025-06-01", "descricao": "Importado", "valor": 200,
            "status_origem": "entregue"})
        cancelado = _pedido(cli)
        dados.cancelar_pedido(cancelado, "x")
        atual = _pedido(cli)
        assert set(_cartoes()) == {atual}
        texto = admin.get("/operacao").text
        assert f'data-id="{hist}"' not in texto and f'data-id="{cancelado}"' not in texto
        # continua em Pedidos, no histórico do cliente e no faturamento
        ids = {(r["tipo"], r["id"]) for r in dados.consultar_pedidos(por_pagina=50)["registros"]}
        assert {("pedido", hist), ("historico", importado)} <= ids
        assert hist in [p["id"] for p in dados.pedidos_cliente(cli)]
        assert dados.faturamento_periodo("2025-01-01", "2025-12-31")["quantidade"] == 2

    def test_finalizado_aparece_por_poucos_dias_e_nao_conta_como_pendente(self, app, admin):
        cli = _cliente()
        recente = _pedido(cli)
        for destino in ("separado", "entregue", "recolhido", "conferido", "finalizado"):
            dados.mover_etapa(recente, destino)
        assert _cartoes()[recente][0] == "finalizado"
        assert dados.indicadores_esteira()["na_esteira"] == 0
        assert recente not in _cartoes(D(dados.DIAS_FINALIZADOS_NA_ESTEIRA + 1))


class TesteFiltrosEIndicadores:
    def test_filtros(self, app, admin):
        a = _pedido(_cliente("Camila Ferreira", "67 99999-1234"), evento=D(0),
                    responsavel="Carlos")
        b = _pedido(_cliente("Rafael Souza"), evento=D(1), itens=[
            {"tipo": "kit", "descricao": "Kit Jardim", "quantidade": 1, "preco_unitario": 100},
            {"tipo": "servico", "descricao": "Montagem", "quantidade": 1, "preco_unitario": 0}])
        c = _pedido(_cliente("Ana Paula"), evento=D(10))
        dados.mover_etapa(c, "separado")
        assert set(_cartoes(evento="hoje")) == {a}
        assert set(_cartoes(evento="amanha")) == {b}
        assert set(_cartoes(evento="proximos")) == {c}
        assert set(_cartoes(servico="Montagem")) == {b}
        assert set(_cartoes(servico="entrega")) == {a, c}
        assert set(_cartoes(responsavel="carlos")) == {a}
        assert set(_cartoes(status="separado")) == {c}
        assert set(_cartoes(q=f"#{b}")) == {b}
        assert set(_cartoes(q="rafael")) == {b}
        assert set(_cartoes(q="1234")) == {a}

    def test_indicadores_respeitam_a_data(self, app, admin):
        cli = _cliente()
        _pedido(cli, evento=D(1), retirada=D(0))
        _pedido(cli, evento=D(2), retirada=D(1))
        assert dados.indicadores_esteira()["entregas_dia"] == 1
        assert dados.indicadores_esteira(D(1))["entregas_dia"] == 1
        assert dados.indicadores_esteira(D(5))["entregas_dia"] == 0

    def test_cartao_mostra_dados_reais(self, app, admin):
        pid = _pedido(_cliente(), itens=[
            {"tipo": "produto", "descricao": "Bandejas", "quantidade": 5, "preco_unitario": 10},
            {"tipo": "kit", "descricao": "Kit Safari", "quantidade": 1, "preco_unitario": 300},
            {"tipo": "servico", "descricao": "Entrega", "quantidade": 1, "preco_unitario": 0},
            {"tipo": "servico", "descricao": "Montagem", "quantidade": 1, "preco_unitario": 0}])
        c = _cartoes()[pid][1]
        assert (c["itens"], c["principal"], c["servicos"]) == (
            6, "Kit Safari", ["Entrega", "Montagem"])
        texto = admin.get("/operacao").text
        assert "6 itens" in texto and "Kit Safari" in texto and "Serviços: Entrega +1" in texto


class TesteTela:
    def test_estrutura_do_figma_e_estado_vazio(self, app, admin):
        r = admin.get("/operacao")
        for texto in ("Esteira de pedidos", "Acompanhe e avance cada pedido pela operação.",
                      "Hoje", "Pedidos na esteira", "Em atraso", "No prazo", "Entregas hoje",
                      "Todos os eventos", "Todos os serviços", "Todos os responsáveis",
                      "Todos os status", "Buscar pedido ou cliente", "Limpar",
                      "Preparação", "Separado", "Entregue / Retirado",
                      "Recolhido / Devolvido", "Conferência", "Finalizado",
                      "Tudo em dia", "Históricos não entram na esteira."):
            assert texto in r.text, texto
        assert r.text.count("+ Adicionar pedido") == 1

    def test_parcial_e_navegacao_de_data(self, app, admin):
        _pedido(_cliente())
        r = admin.get(f"/operacao?parcial=1&data={D(1)}")
        assert 'data-parte="topo"' in r.text and 'data-parte="kanban"' in r.text
        assert "<html" not in r.text and f'data-dia="{D(1)}"' in r.text
        assert admin.get("/operacao?data=invalida").status_code == 200

    def test_agenda_reflete_a_esteira(self, app, admin):
        pid = _pedido(_cliente(), evento=D(2))
        _mover(admin, pid, "separado", "preparacao")
        evento = [e for e in dados.eventos_agenda(D(2), D(2)) if e["id"] == pid][0]
        assert evento["status_operacional"] == "separado"


class TestePermissoes:
    def test_quem_ve_quem_move_quem_finaliza(self, app, admin):
        pid = _pedido(_cliente())
        comercial = _entrar(app, "comercial")
        assert comercial.get("/operacao").status_code == 403
        visual = _entrar(app, "visualizacao")
        assert visual.get("/operacao").status_code == 200
        assert "est-form-acao" not in visual.get("/operacao").text
        assert _mover(visual, pid, "separado", "preparacao")[0] == 403
        operacional = _entrar(app, "operacional")
        assert _mover(operacional, pid, "separado", "preparacao")[0] == 200
        for destino in ("entregue", "recolhido", "conferido", "finalizado"):
            assert _mover(operacional, pid, destino)[0] == 200
        assert _status(pid) == ("finalizado", "finalizado")

    def test_outra_empresa_nao_ve_nem_move(self, app, admin):
        pid = _pedido(_cliente())
        b = dados.criar_empresa("Empresa B")
        with dados.usando_tenant(b):
            dados.salvar_usuario({"nome": "B", "login": "b_op", "perfil": "operacional"},
                                 senha_hash=generate_password_hash("x"))
        c = app.test_client()
        c.post("/entrar", data={"login": "b_op", "senha": "x"})
        assert f'data-id="{pid}"' not in c.get("/operacao").text
        codigo, res = _mover(c, pid, "separado", "preparacao")
        assert codigo == 409 and "não encontrado" in res["mensagem"]
        assert _status(pid)[1] == "preparacao"


class TesteOcorrencias:
    def test_ocorrencia_com_tipo_vai_para_a_timeline(self, app, admin):
        pid = _pedido(_cliente())
        r = admin.post(f"/pedido/{pid}/ocorrencia",
                       data={"tipo": "Item danificado", "texto": "Mesa riscada",
                             "voltar": "/operacao"})
        assert r.headers["Location"] == "/operacao"
        e = [x for x in dados.buscar_pedido_detalhe(pid)["linha_do_tempo"]
             if x["categoria"] == "Ocorrência"][0]
        assert (e["titulo"], e["detalhe"]) == ("Ocorrência: Item danificado", "Mesa riscada")
        assert "Item danificado" in admin.get(f"/pedido/{pid}").text
