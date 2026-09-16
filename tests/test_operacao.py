"""Testes do Sprint 09 — Operacao."""

from sistema import dados


def _cliente(admin, nome="Ana Souza"):
    admin.post("/cliente", data={"nome": nome, "whatsapp": ""},
               follow_redirects=True)
    return [c for c in dados.listar_clientes() if c["nome"] == nome][0]


def _pedido(admin, cliente_id, **extra):
    form = {
        "cliente_id": str(cliente_id),
        "data_evento": extra.get("data_evento", "2026-10-15"),
        "data_retirada": extra.get("data_retirada", "2026-10-14"),
        "data_devolucao": extra.get("data_devolucao", "2026-10-16"),
        "status_comercial": extra.get("status_comercial", "confirmado"),
        "status_operacional": extra.get("status_operacional", "preparacao"),
        "observacoes": "",
        "item_tipo_0": "produto",
        "item_item_id_0": "",
        "item_descricao_0": "Mesa redonda",
        "item_quantidade_0": "2",
        "item_preco_0": "50.00",
    }
    admin.post("/pedido/novo", data=form, follow_redirects=True)
    return dados.listar_pedidos()[-1]


# --- dados.listar_pedidos_operacional ---

class TesteListarPedidosOperacional:
    def test_lista_todos_nao_cancelados(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        peds = dados.listar_pedidos_operacional()
        assert len(peds) == 1

    def test_filtra_por_status(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        peds = dados.listar_pedidos_operacional(status_operacional="preparacao")
        assert len(peds) == 1
        peds = dados.listar_pedidos_operacional(status_operacional="separado")
        assert len(peds) == 0

    def test_busca_por_cliente(self, app, admin):
        cli = _cliente(admin, "Maria Oliveira")
        _pedido(admin, cli["id"])
        peds = dados.listar_pedidos_operacional(busca="Maria")
        assert len(peds) == 1
        peds = dados.listar_pedidos_operacional(busca="Inexistente")
        assert len(peds) == 0

    def test_cancelado_nao_aparece(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        dados.cancelar_pedido(ped["id"])
        peds = dados.listar_pedidos_operacional()
        assert len(peds) == 0


# --- dados.avancar_status_operacional ---

class TesteAvancarStatus:
    def test_avanca_preparacao_para_separado(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        novo = dados.avancar_status_operacional(ped["id"])
        assert novo == "separado"
        ped = dados.buscar_pedido_festas(ped["id"])
        assert ped["status_operacional"] == "separado"

    def test_fluxo_completo(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        esperado = ["separado", "montado", "entregue", "recolhido", "conferido"]
        for s in esperado:
            novo = dados.avancar_status_operacional(ped["id"])
            assert novo == s

    def test_conferido_nao_avanca(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        for _ in range(5):
            dados.avancar_status_operacional(ped["id"])
        try:
            dados.avancar_status_operacional(ped["id"])
            assert False, "Deveria lancar ValueError"
        except ValueError:
            pass

    def test_cancelado_nao_avanca(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        dados.cancelar_pedido(ped["id"])
        try:
            dados.avancar_status_operacional(ped["id"])
            assert False, "Deveria lancar ValueError"
        except ValueError:
            pass

    def test_pedido_inexistente(self, app, admin):
        try:
            dados.avancar_status_operacional(9999)
            assert False, "Deveria lancar ValueError"
        except ValueError:
            pass

    def test_observacao_grava_audit(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        dados.avancar_status_operacional(ped["id"], "Item quebrado")
        with dados.conectar() as conn:
            logs = conn.execute(
                "SELECT * FROM audit_log WHERE tipo='operacao'"
            ).fetchall()
            assert len(logs) == 1
            assert "Item quebrado" in logs[0]["dados"]


# --- rotas operacao ---

class TesteRotasOperacao:
    def test_painel_operacional_200(self, app, admin):
        r = admin.get("/operacao")
        assert r.status_code == 200
        assert "Esteira de pedidos" in r.text

    def test_painel_mostra_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        r = admin.get("/operacao")
        assert cli["nome"] in r.text

    def test_painel_filtro_status(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        r = admin.get("/operacao?status=preparacao")
        assert r.status_code == 200
        assert cli["nome"] in r.text
        r = admin.get("/operacao?status=conferido")
        assert r.status_code == 200

    def test_painel_busca(self, app, admin):
        cli = _cliente(admin, "Carlos Silva")
        _pedido(admin, cli["id"])
        r = admin.get("/operacao?q=Carlos")
        assert r.status_code == 200
        assert "Carlos" in r.text

    def test_ver_pedido_operacional(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        r = admin.get(f"/operacao/pedido/{ped['id']}")
        assert r.status_code == 200
        assert "Avancar para separado" in r.text

    def test_ver_pedido_inexistente_404(self, app, admin):
        r = admin.get("/operacao/pedido/9999")
        assert r.status_code == 404

    def test_avancar_via_rota(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        r = admin.post(f"/operacao/pedido/{ped['id']}/avancar",
                       data={"observacao": ""},
                       follow_redirects=True)
        assert r.status_code == 200
        assert "separado" in r.text

    def test_avancar_com_observacao(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        r = admin.post(f"/operacao/pedido/{ped['id']}/avancar",
                       data={"observacao": "Tudo ok"},
                       follow_redirects=True)
        assert r.status_code == 200

    def test_pedido_conferido_sem_botao_avancar(self, app, admin):
        cli = _cliente(admin)
        ped = _pedido(admin, cli["id"])
        for _ in range(5):
            dados.avancar_status_operacional(ped["id"])
        r = admin.get(f"/operacao/pedido/{ped['id']}")
        assert "Pedido concluido" in r.text
        assert "Avancar" not in r.text

    def test_menu_operacao_tem_pedidos(self, app, admin):
        r = admin.get("/operacao")
        assert "Pedidos" in r.text


# --- permissoes ---

class TestePermissoesOperacao:
    def test_operacional_acessa_painel(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Op", "login": "operador", "perfil": "operacional"},
            senha_hash=generate_password_hash("op123"))
        client.post("/entrar", data={"login": "operador", "senha": "op123"})
        r = client.get("/operacao")
        assert r.status_code == 200

    def test_comercial_nao_acessa_painel(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Com", "login": "comercial", "perfil": "comercial"},
            senha_hash=generate_password_hash("com123"))
        client.post("/entrar", data={"login": "comercial", "senha": "com123"})
        r = client.get("/operacao")
        assert r.status_code == 403

    def test_gestor_acessa_painel(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Ges", "login": "gestor", "perfil": "gestor"},
            senha_hash=generate_password_hash("ges123"))
        client.post("/entrar", data={"login": "gestor", "senha": "ges123"})
        r = client.get("/operacao")
        assert r.status_code == 200
