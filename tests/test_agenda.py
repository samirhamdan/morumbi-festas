"""Testes do Sprint 08 — Agenda."""

from sistema import dados


def _cliente(admin, nome="Ana Souza"):
    admin.post("/cliente", data={"nome": nome, "whatsapp": ""},
               follow_redirects=True)
    return [c for c in dados.listar_clientes() if c["nome"] == nome][0]


def _pedido(admin, cliente_id, data_evento="2026-10-15",
            data_ret="2026-10-14", data_dev="2026-10-16", **extra):
    form = {
        "cliente_id": str(cliente_id),
        "data_evento": data_evento,
        "data_retirada": data_ret,
        "data_devolucao": data_dev,
        "status_comercial": extra.get("status_comercial", "confirmado"),
        "status_operacional": extra.get("status_operacional", "preparacao"),
        "observacoes": "",
        "item_tipo_0": "produto",
        "item_item_id_0": "",
        "item_descricao_0": "Item teste",
        "item_quantidade_0": "1",
        "item_preco_0": "100.00",
    }
    return admin.post("/pedido/novo", data=form, follow_redirects=True)


# --- dados.eventos_agenda ---

class TesteEventosAgenda:
    def test_retorna_pedidos_no_periodo(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31")
        assert len(evts) == 1
        assert evts[0]["cliente_nome"] == cli["nome"]

    def test_fora_do_periodo_nao_retorna(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-11-01", "2026-11-30")
        assert len(evts) == 0

    def test_filtro_tipo_evento(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15",
                data_ret="2026-10-14", data_dev="2026-10-16")
        evts = dados.eventos_agenda("2026-10-15", "2026-10-15", tipo="evento")
        assert len(evts) == 1
        evts = dados.eventos_agenda("2026-10-14", "2026-10-14", tipo="evento")
        assert len(evts) == 0

    def test_filtro_tipo_retirada(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-10-14", "2026-10-14", tipo="retirada")
        assert len(evts) == 1
        evts = dados.eventos_agenda("2026-10-15", "2026-10-15", tipo="retirada")
        assert len(evts) == 0

    def test_filtro_tipo_devolucao(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-10-16", "2026-10-16", tipo="devolucao")
        assert len(evts) == 1

    def test_filtro_status_comercial(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], status_comercial="confirmado")
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31",
                                    status_comercial="confirmado")
        assert len(evts) == 1
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31",
                                    status_comercial="entregue")
        assert len(evts) == 0

    def test_cancelado_nao_aparece(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        peds = dados.listar_pedidos()
        dados.cancelar_pedido(peds[0]["id"])
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31")
        assert len(evts) == 0

    def test_multiplos_pedidos(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-10",
                data_ret="2026-10-09", data_dev="2026-10-11")
        _pedido(admin, cli["id"], data_evento="2026-10-20",
                data_ret="2026-10-19", data_dev="2026-10-21")
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31")
        assert len(evts) == 2


# --- rotas agenda ---

class TesteRotaAgenda:
    def test_agenda_mensal_200(self, app, admin):
        r = admin.get("/agenda")
        assert r.status_code == 200
        assert "Agenda" in r.text

    def test_agenda_mensal_com_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15")
        r = admin.get("/agenda?visao=mensal&ano=2026&mes=10")
        assert r.status_code == 200
        assert cli["nome"] in r.text or "1" in r.text

    def test_agenda_semanal_200(self, app, admin):
        r = admin.get("/agenda?visao=semanal&ano=2026&mes=10&dia=15")
        assert r.status_code == 200
        assert "Seg" in r.text

    def test_agenda_semanal_com_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15",
                data_ret="2026-10-14", data_dev="2026-10-16")
        r = admin.get("/agenda?visao=semanal&ano=2026&mes=10&dia=14")
        assert r.status_code == 200
        assert cli["nome"] in r.text

    def test_agenda_diaria_200(self, app, admin):
        r = admin.get("/agenda?visao=diaria&ano=2026&mes=10&dia=15")
        assert r.status_code == 200
        assert "Retiradas" in r.text
        assert "Devoluções" in r.text
        assert "Eventos" in r.text

    def test_agenda_diaria_com_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15",
                data_ret="2026-10-14", data_dev="2026-10-16")
        r = admin.get("/agenda?visao=diaria&ano=2026&mes=10&dia=14")
        assert r.status_code == 200
        assert cli["nome"] in r.text

    def test_agenda_filtro_tipo(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        r = admin.get("/agenda?visao=mensal&ano=2026&mes=10&tipo=retirada")
        assert r.status_code == 200

    def test_agenda_filtro_status(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        r = admin.get("/agenda?visao=mensal&ano=2026&mes=10&status=confirmado")
        assert r.status_code == 200

    def test_agenda_sem_login_redireciona(self, app, client):
        r = client.get("/agenda")
        assert r.status_code == 302

    def test_agenda_menu_operacao(self, app, admin):
        r = admin.get("/agenda")
        assert "Operação" in r.text


# --- _eventos_do_dia ---

class TesteEventosDoDia:
    def test_classifica_tipos(self, app):
        from sistema.app import _eventos_do_dia
        eventos = [
            {"id": 1, "data_evento": "2026-10-15",
             "data_retirada": "2026-10-14", "data_devolucao": "2026-10-16",
             "cliente_nome": "Ana"},
        ]
        r = _eventos_do_dia(eventos, "2026-10-15")
        assert len(r) == 1
        assert "evento" in r[0]["tipos_dia"]

        r = _eventos_do_dia(eventos, "2026-10-14")
        assert "retirada" in r[0]["tipos_dia"]

        r = _eventos_do_dia(eventos, "2026-10-16")
        assert "devolucao" in r[0]["tipos_dia"]

    def test_dia_sem_evento(self, app):
        from sistema.app import _eventos_do_dia
        eventos = [
            {"id": 1, "data_evento": "2026-10-15",
             "data_retirada": "2026-10-14", "data_devolucao": "2026-10-16",
             "cliente_nome": "Ana"},
        ]
        r = _eventos_do_dia(eventos, "2026-10-17")
        assert len(r) == 0

    def test_mesmo_dia_evento_e_retirada(self, app):
        from sistema.app import _eventos_do_dia
        eventos = [
            {"id": 1, "data_evento": "2026-10-15",
             "data_retirada": "2026-10-15", "data_devolucao": "2026-10-17",
             "cliente_nome": "Ana"},
        ]
        r = _eventos_do_dia(eventos, "2026-10-15")
        assert len(r) == 1
        assert "evento" in r[0]["tipos_dia"]
        assert "retirada" in r[0]["tipos_dia"]
