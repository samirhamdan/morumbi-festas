"""Testes do modulo comercial — Sprint 06."""

from sistema import dados


def _cliente(admin, nome="Maria Silva"):
    admin.post("/cliente", data={
        "nome": nome, "whatsapp": "",
    }, follow_redirects=True)
    return [c for c in dados.listar_clientes() if c["nome"] == nome][0]


def _lead(admin, cliente_id, interesse="Festa infantil", origem_id=None):
    admin.post("/lead", data={
        "cliente_id": str(cliente_id),
        "origem_id": str(origem_id or ""),
        "interesse": interesse,
        "valor_estimado": "500.00",
        "status": "novo",
    }, follow_redirects=True)
    leads = dados.listar_leads()
    return [l for l in leads if l["cliente_id"] == cliente_id
            and l["interesse"] == interesse][0]


# ---- Origens de lead ----

class TesteOrigens:
    def test_sementes_criadas(self, app):
        origens = dados.listar_origens()
        nomes = [o["nome"] for o in origens]
        assert "Instagram" in nomes
        assert "WhatsApp" in nomes
        assert len(origens) >= 7

    def test_criar_origem(self, app, admin):
        admin.post("/origem", data={"nome": "Outdoor"},
                   follow_redirects=True)
        nomes = [o["nome"] for o in dados.listar_origens()]
        assert "Outdoor" in nomes

    def test_origem_nome_obrigatorio(self, app, admin):
        r = admin.post("/origem", data={"nome": ""},
                       follow_redirects=True)
        assert "obrigatório" in r.text.lower()

    def test_origem_duplicada_recusada(self, app, admin):
        dados.salvar_origem("Teste Dup")
        r = admin.post("/origem", data={"nome": "Teste Dup"},
                       follow_redirects=True)
        assert "Já existe" in r.text

    def test_excluir_origem_livre(self, app, admin):
        oid = dados.salvar_origem("Para Excluir")
        admin.post(f"/origem/{oid}/excluir", follow_redirects=True)
        nomes = [o["nome"] for o in dados.listar_origens()]
        assert "Para Excluir" not in nomes

    def test_excluir_origem_em_uso_recusada(self, app, admin):
        c = _cliente(admin, "Cli Origem")
        oid = dados.salvar_origem("Em Uso")
        dados.salvar_lead({"cliente_id": c["id"], "origem_id": oid,
                           "status": "novo"})
        r = admin.post(f"/origem/{oid}/excluir", follow_redirects=True)
        assert "em uso" in r.text.lower()

    def test_lista_origens_carrega(self, app, admin):
        r = admin.get("/origens")
        assert r.status_code == 200
        assert "Origens" in r.text

    def test_origens_exige_admin(self, app, client):
        r = client.get("/origens")
        assert r.status_code in (302, 303)


# ---- Leads ----

class TesteCriarLead:
    def test_criar_lead_basico(self, app, admin):
        c = _cliente(admin)
        r = admin.post("/lead", data={
            "cliente_id": str(c["id"]),
            "interesse": "Casamento",
            "valor_estimado": "3000.00",
            "status": "novo",
        }, follow_redirects=True)
        assert r.status_code == 200
        leads = dados.listar_leads()
        assert any(l["interesse"] == "Casamento" for l in leads)

    def test_lead_sem_cliente_recusado(self, app, admin):
        r = admin.post("/lead", data={
            "cliente_id": "",
            "interesse": "Teste",
            "status": "novo",
        }, follow_redirects=True)
        assert "obrigatório" in r.text.lower()

    def test_lead_status_padrao_novo(self, app, admin):
        c = _cliente(admin, "Cli Status")
        dados.salvar_lead({"cliente_id": c["id"], "status": "novo"})
        lead = dados.listar_leads()[0]
        assert lead["status"] == "novo"

    def test_lead_com_origem(self, app, admin):
        c = _cliente(admin, "Cli Origem2")
        origens = dados.listar_origens()
        oid = origens[0]["id"]
        lead = _lead(admin, c["id"], "Com origem", oid)
        assert lead["origem_id"] == oid


class TesteEditarLead:
    def test_editar_lead(self, app, admin):
        c = _cliente(admin, "Cli Edit")
        lead = _lead(admin, c["id"])
        r = admin.post(f"/lead/{lead['id']}", data={
            "cliente_id": str(c["id"]),
            "interesse": "Festa atualizada",
            "valor_estimado": "800.00",
            "status": "atendimento",
        }, follow_redirects=True)
        assert r.status_code == 200
        atualizado = dados.buscar_lead(lead["id"])
        assert atualizado["interesse"] == "Festa atualizada"
        assert atualizado["status"] == "atendimento"

    def test_lead_inexistente_404(self, app, admin):
        r = admin.get("/lead/9999")
        assert r.status_code == 404


class TesteMoverLead:
    def test_mover_para_proximo_status(self, app, admin):
        c = _cliente(admin, "Cli Mover")
        lead = _lead(admin, c["id"])
        assert lead["status"] == "novo"
        dados.mover_lead(lead["id"], "atendimento")
        atualizado = dados.buscar_lead(lead["id"])
        assert atualizado["status"] == "atendimento"

    def test_mover_para_perdido(self, app, admin):
        c = _cliente(admin, "Cli Perdido")
        lead = _lead(admin, c["id"])
        dados.mover_lead(lead["id"], "perdido")
        assert dados.buscar_lead(lead["id"])["status"] == "perdido"

    def test_mover_status_invalido_recusado(self, app, admin):
        c = _cliente(admin, "Cli Invalido")
        lead = _lead(admin, c["id"])
        try:
            dados.mover_lead(lead["id"], "inexistente")
            assert False, "Deveria ter lançado ValueError"
        except ValueError:
            pass

    def test_mover_via_rota(self, app, admin):
        c = _cliente(admin, "Cli Rota")
        lead = _lead(admin, c["id"])
        r = admin.post(f"/lead/{lead['id']}/mover", data={
            "status": "orcamento", "retorno": "kanban",
        }, follow_redirects=True)
        assert r.status_code == 200
        assert dados.buscar_lead(lead["id"])["status"] == "orcamento"


class TesteFunilKanban:
    def test_kanban_carrega(self, app, admin):
        r = admin.get("/leads?modo=kanban")
        assert r.status_code == 200
        assert "Funil" in r.text

    def test_kanban_mostra_etapas(self, app, admin):
        r = admin.get("/leads?modo=kanban")
        for etapa in dados.ETAPAS_LEAD:
            assert etapa.capitalize() in r.text

    def test_kanban_mostra_lead(self, app, admin):
        c = _cliente(admin, "Cli Kanban")
        _lead(admin, c["id"], "Festa kanban")
        r = admin.get("/leads?modo=kanban")
        assert "Cli Kanban" in r.text

    def test_contadores(self, app, admin):
        c = _cliente(admin, "Cli Cont")
        _lead(admin, c["id"], "Lead 1")
        contadores = dados.contadores_lead()
        assert contadores.get("novo", 0) >= 1

    def test_leads_por_etapa(self, app, admin):
        c = _cliente(admin, "Cli Etapa")
        _lead(admin, c["id"], "Etapa test")
        funil = dados.leads_por_etapa()
        assert len(funil["novo"]) >= 1


class TesteListaLeads:
    def test_lista_carrega(self, app, admin):
        r = admin.get("/leads?modo=lista")
        assert r.status_code == 200
        assert "Leads" in r.text

    def test_lista_filtra_por_origem(self, app, admin):
        c = _cliente(admin, "Cli Filtro")
        ori = dados.listar_origens()
        _lead(admin, c["id"], "Com Instagram", ori[0]["id"])
        r = admin.get(f"/leads?modo=lista&origem={ori[0]['nome']}")
        assert "Cli Filtro" in r.text

    def test_lista_busca(self, app, admin):
        c = _cliente(admin, "Cli Busca Lead")
        _lead(admin, c["id"], "Aniversario 15 anos")
        r = admin.get("/leads?modo=lista&q=aniversario")
        assert "Aniversario" in r.text

    def test_leads_exige_perfil(self, app, client):
        r = client.get("/leads")
        assert r.status_code in (302, 303)


# ---- Orcamentos ----

class TesteCriarOrcamento:
    def test_criar_orcamento_basico(self, app, admin):
        c = _cliente(admin, "Cli Orc")
        r = admin.post("/orcamento", data={
            "cliente_id": str(c["id"]),
            "desconto": "0",
            "status": "rascunho",
            "item_tipo_0": "servico",
            "item_descricao_0": "Montagem",
            "item_quantidade_0": "1",
            "item_preco_0": "200.00",
        }, follow_redirects=True)
        assert r.status_code == 200
        orcs = dados.listar_orcamentos()
        assert len(orcs) >= 1
        assert orcs[0]["total"] == 200.0

    def test_orcamento_sem_cliente_recusado(self, app, admin):
        r = admin.post("/orcamento", data={
            "cliente_id": "",
            "desconto": "0",
            "status": "rascunho",
        }, follow_redirects=True)
        assert "obrigatório" in r.text.lower()

    def test_orcamento_com_desconto(self, app, admin):
        c = _cliente(admin, "Cli Desc")
        admin.post("/orcamento", data={
            "cliente_id": str(c["id"]),
            "desconto": "50.00",
            "status": "rascunho",
            "item_tipo_0": "produto",
            "item_descricao_0": "Mesa",
            "item_quantidade_0": "2",
            "item_preco_0": "100.00",
        }, follow_redirects=True)
        orc = dados.listar_orcamentos()[0]
        assert orc["subtotal"] == 200.0
        assert orc["desconto"] == 50.0
        assert orc["total"] == 150.0

    def test_desconto_nao_negativo(self, app):
        c_id = dados.salvar_cliente({"nome": "Teste Neg"})
        try:
            dados.salvar_orcamento(
                {"cliente_id": c_id, "desconto": -10, "status": "rascunho"},
                [])
            assert False, "Deveria recusar desconto negativo"
        except dados.ErroDeCampo:
            pass

    def test_desconto_nao_ultrapassa_subtotal(self, app, admin):
        c = _cliente(admin, "Cli Max Desc")
        admin.post("/orcamento", data={
            "cliente_id": str(c["id"]),
            "desconto": "500.00",
            "status": "rascunho",
            "item_tipo_0": "servico",
            "item_descricao_0": "Item",
            "item_quantidade_0": "1",
            "item_preco_0": "100.00",
        }, follow_redirects=True)
        orc = dados.listar_orcamentos()[0]
        assert orc["total"] == 0

    def test_orcamento_multiplos_itens(self, app, admin):
        c = _cliente(admin, "Cli Multi")
        admin.post("/orcamento", data={
            "cliente_id": str(c["id"]),
            "desconto": "0",
            "status": "rascunho",
            "item_tipo_0": "produto",
            "item_descricao_0": "Mesa",
            "item_quantidade_0": "1",
            "item_preco_0": "100.00",
            "item_tipo_1": "kit",
            "item_descricao_1": "Kit festa",
            "item_quantidade_1": "1",
            "item_preco_1": "300.00",
        }, follow_redirects=True)
        orc = dados.listar_orcamentos()[0]
        assert len(orc["itens"]) == 2
        assert orc["subtotal"] == 400.0


class TesteEditarOrcamento:
    def test_editar_orcamento(self, app, admin):
        c = _cliente(admin, "Cli Edit Orc")
        oid = dados.salvar_orcamento(
            {"cliente_id": c["id"], "desconto": 0, "status": "rascunho"},
            [{"tipo": "servico", "descricao": "Montagem",
              "quantidade": 1, "preco_unitario": 100}])
        r = admin.post(f"/orcamento/{oid}", data={
            "cliente_id": str(c["id"]),
            "desconto": "10.00",
            "status": "enviado",
            "item_tipo_0": "servico",
            "item_descricao_0": "Montagem completa",
            "item_quantidade_0": "1",
            "item_preco_0": "150.00",
        }, follow_redirects=True)
        assert r.status_code == 200
        orc = dados.buscar_orcamento(oid)
        assert orc["status"] == "enviado"
        assert orc["subtotal"] == 150.0

    def test_orcamento_inexistente_404(self, app, admin):
        r = admin.get("/orcamento/9999")
        assert r.status_code == 404


class TesteListaOrcamentos:
    def test_lista_carrega(self, app, admin):
        r = admin.get("/orcamentos")
        assert r.status_code == 200
        assert "Orçamentos" in r.text

    def test_lista_filtra_por_status(self, app, admin):
        c = _cliente(admin, "Cli Status Orc")
        dados.salvar_orcamento(
            {"cliente_id": c["id"], "desconto": 0, "status": "enviado"},
            [{"tipo": "servico", "descricao": "Item",
              "quantidade": 1, "preco_unitario": 100}])
        r = admin.get("/orcamentos?status=enviado")
        assert "Cli Status Orc" in r.text

    def test_lista_vazia(self, app, admin):
        r = admin.get("/orcamentos")
        assert "Nenhum orçamento" in r.text

    def test_orcamentos_exige_perfil(self, app, client):
        r = client.get("/orcamentos")
        assert r.status_code in (302, 303)


# ---- Conversao orcamento -> pedido ----

class TesteConversao:
    def test_converter_cria_pedido(self, app, admin):
        c = _cliente(admin, "Cli Conv")
        oid = dados.salvar_orcamento(
            {"cliente_id": c["id"], "desconto": 0, "status": "rascunho"},
            [{"tipo": "produto", "descricao": "Cadeira",
              "quantidade": 10, "preco_unitario": 30}])
        r = admin.post(f"/orcamento/{oid}/converter",
                       follow_redirects=True)
        assert r.status_code == 200
        assert "Pedido" in r.text
        peds = dados.listar_pedidos()
        assert len(peds) >= 1
        assert peds[0]["itens"][0]["descricao"] == "Cadeira"

    def test_converter_atualiza_orcamento(self, app, admin):
        c = _cliente(admin, "Cli Conv2")
        oid = dados.salvar_orcamento(
            {"cliente_id": c["id"], "desconto": 0, "status": "enviado"},
            [{"tipo": "servico", "descricao": "Item",
              "quantidade": 1, "preco_unitario": 50}])
        dados.converter_orcamento_em_pedido(oid)
        orc = dados.buscar_orcamento(oid)
        assert orc["status"] == "aceito"

    def test_converter_atualiza_lead(self, app, admin):
        c = _cliente(admin, "Cli Conv Lead")
        lead_id = dados.salvar_lead({
            "cliente_id": c["id"], "interesse": "Festa", "status": "negociacao"})
        oid = dados.salvar_orcamento(
            {"cliente_id": c["id"], "lead_id": lead_id,
             "desconto": 0, "status": "enviado"},
            [{"tipo": "servico", "descricao": "Item",
              "quantidade": 1, "preco_unitario": 100}])
        dados.converter_orcamento_em_pedido(oid)
        lead = dados.buscar_lead(lead_id)
        assert lead["status"] == "contratado"

    def test_converter_orcamento_recusado_recusado(self, app):
        c_id = dados.salvar_cliente({"nome": "Teste Recusado"})
        oid = dados.salvar_orcamento(
            {"cliente_id": c_id, "desconto": 0, "status": "recusado"},
            [{"tipo": "servico", "descricao": "Item",
              "quantidade": 1, "preco_unitario": 50}])
        try:
            dados.converter_orcamento_em_pedido(oid)
            assert False, "Deveria recusar"
        except ValueError:
            pass

    def test_converter_copia_itens(self, app, admin):
        c = _cliente(admin, "Cli Copia")
        oid = dados.salvar_orcamento(
            {"cliente_id": c["id"], "desconto": 0, "status": "rascunho"},
            [{"tipo": "produto", "descricao": "Mesa", "quantidade": 2,
              "preco_unitario": 80},
             {"tipo": "kit", "descricao": "Kit completo", "quantidade": 1,
              "preco_unitario": 500}])
        pid = dados.converter_orcamento_em_pedido(oid)
        ped = dados.buscar_pedido_festas(pid)
        assert len(ped["itens"]) == 2
        assert ped["total"] == 660.0


# ---- Pedidos ----

class TestePedidos:
    def test_lista_pedidos_carrega(self, app, admin):
        r = admin.get("/pedidos")
        assert r.status_code == 200
        assert "Pedidos" in r.text

    def test_lista_pedidos_vazia(self, app, admin):
        r = admin.get("/pedidos")
        assert "Nenhum pedido" in r.text

    def test_ver_pedido(self, app, admin):
        c = _cliente(admin, "Cli Ver Ped")
        oid = dados.salvar_orcamento(
            {"cliente_id": c["id"], "desconto": 0, "status": "rascunho"},
            [{"tipo": "servico", "descricao": "Servico",
              "quantidade": 1, "preco_unitario": 200}])
        pid = dados.converter_orcamento_em_pedido(oid)
        r = admin.get(f"/pedido/{pid}")
        assert r.status_code == 200
        assert "Pedido" in r.text
        assert "Servico" in r.text

    def test_pedido_inexistente_404(self, app, admin):
        r = admin.get("/pedido/9999")
        assert r.status_code == 404

    def test_pedidos_exige_perfil(self, app, client):
        r = client.get("/pedidos")
        assert r.status_code in (302, 303)


# ---- Rotas do menu ----

class TesteRotasComerciais:
    def test_todas_novas_rotas_no_menu(self, admin):
        from sistema.app import MENU
        endpoints = []
        for grupo, itens in MENU:
            for ep, _ in itens:
                endpoints.append(ep)
        assert "lista_leads" in endpoints
        assert "lista_orcamentos" in endpoints
        assert "lista_pedidos" in endpoints
        assert "lista_origens" in endpoints
