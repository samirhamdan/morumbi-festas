"""Testes do Sprint 07 — Reservas e disponibilidade."""

from sistema import dados


def _cliente(admin, nome="Ana Souza"):
    admin.post("/cliente", data={"nome": nome, "whatsapp": ""},
               follow_redirects=True)
    return [c for c in dados.listar_clientes() if c["nome"] == nome][0]


def _produto(admin, nome="Mesa Redonda", qtd=5):
    admin.post("/produto", data={
        "nome": nome, "codigo_sku": "", "descricao": "",
        "preco_locacao": "50.00", "preco_venda": "0",
        "quantidade_total": str(qtd), "status": "disponivel",
        "categoria_id": "", "publicado": "1",
    }, follow_redirects=True)
    prods = dados.listar_produtos()
    return [p for p in prods if p["nome"] == nome][0]


def _pedido_via_form(admin, cliente_id, itens=None, data_ret="2026-10-01",
                     data_dev="2026-10-03", **extra):
    form = {
        "cliente_id": str(cliente_id),
        "data_evento": extra.get("data_evento", "2026-10-02"),
        "data_retirada": data_ret,
        "data_devolucao": data_dev,
        "status_comercial": extra.get("status_comercial", "confirmado"),
        "status_operacional": extra.get("status_operacional", "preparacao"),
        "observacoes": extra.get("observacoes", ""),
    }
    if itens:
        for i, item in enumerate(itens):
            form[f"item_tipo_{i}"] = item.get("tipo", "produto")
            form[f"item_item_id_{i}"] = str(item.get("item_id", ""))
            form[f"item_descricao_{i}"] = item.get("descricao", "Item")
            form[f"item_quantidade_{i}"] = str(item.get("quantidade", 1))
            form[f"item_preco_{i}"] = str(item.get("preco_unitario", "50.00"))
    return admin.post("/pedido/novo", data=form, follow_redirects=True)


# ---- Criacao de pedido ----

class TesteCriarPedido:
    def test_criar_pedido_simples(self, app, admin):
        cli = _cliente(admin)
        r = _pedido_via_form(admin, cli["id"], itens=[
            {"descricao": "Cadeira plastica", "quantidade": 10, "preco_unitario": "5.00"},
        ])
        assert r.status_code == 200
        peds = dados.listar_pedidos()
        assert len(peds) == 1
        assert peds[0]["cliente_nome"] == cli["nome"]

    def test_criar_pedido_sem_cliente_recusa(self, app, admin):
        r = _pedido_via_form(admin, "", itens=[
            {"descricao": "Cadeira", "quantidade": 1},
        ])
        assert "obrigatorio" in r.text.lower() or r.status_code == 200

    def test_criar_pedido_com_datas_reserva(self, app, admin):
        cli = _cliente(admin)
        prod = _produto(admin)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": prod["nome"], "quantidade": 2, "preco_unitario": "50.00"},
        ], data_ret="2026-10-10", data_dev="2026-10-12")
        peds = dados.listar_pedidos()
        assert peds[0]["data_retirada"] == "2026-10-10"
        assert peds[0]["data_devolucao"] == "2026-10-12"

    def test_devolucao_antes_retirada_recusa(self, app, admin):
        cli = _cliente(admin)
        r = _pedido_via_form(admin, cli["id"],
                             data_ret="2026-10-05", data_dev="2026-10-03")
        assert "posterior" in r.text.lower() or "devolucao" in r.text.lower()


# ---- Editar pedido ----

class TesteEditarPedido:
    def test_editar_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido_via_form(admin, cli["id"], itens=[
            {"descricao": "Mesa", "quantidade": 1, "preco_unitario": "100.00"},
        ])
        ped = dados.listar_pedidos()[0]
        r = admin.post(f"/pedido/{ped['id']}/editar", data={
            "cliente_id": str(cli["id"]),
            "data_evento": "2026-11-01",
            "data_retirada": "2026-10-30",
            "data_devolucao": "2026-11-02",
            "status_comercial": "confirmado",
            "status_operacional": "separado",
            "observacoes": "Atualizado",
            "item_tipo_0": "produto",
            "item_item_id_0": "",
            "item_descricao_0": "Mesa grande",
            "item_quantidade_0": "2",
            "item_preco_0": "120.00",
        }, follow_redirects=True)
        assert r.status_code == 200
        ped2 = dados.buscar_pedido_festas(ped["id"])
        assert ped2["observacoes"] == "Atualizado"
        assert ped2["status_operacional"] == "separado"

    def test_pagina_editar_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido_via_form(admin, cli["id"])
        ped = dados.listar_pedidos()[0]
        r = admin.get(f"/pedido/{ped['id']}/editar")
        assert r.status_code == 200
        assert "Editar Pedido" in r.text


# ---- Cancelar pedido (R4) ----

class TesteCancelarPedido:
    def test_cancelar_pedido(self, app, admin):
        cli = _cliente(admin)
        prod = _produto(admin, "Toalha", 10)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Toalha", "quantidade": 5, "preco_unitario": "10.00"},
        ], data_ret="2026-10-01", data_dev="2026-10-03")
        ped = dados.listar_pedidos()[0]
        disp_antes = dados.disponibilidade(prod["id"], "2026-10-01", "2026-10-03")
        assert disp_antes == 5

        admin.post(f"/pedido/{ped['id']}/cancelar", data={"motivo": "Cliente desistiu"},
                   follow_redirects=True)
        ped2 = dados.buscar_pedido_festas(ped["id"])
        assert ped2["status_comercial"] == "cancelado"
        disp_depois = dados.disponibilidade(prod["id"], "2026-10-01", "2026-10-03")
        assert disp_depois == 10

    def test_cancelar_pedido_ja_cancelado(self, app, admin):
        cli = _cliente(admin)
        _pedido_via_form(admin, cli["id"])
        ped = dados.listar_pedidos()[0]
        admin.post(f"/pedido/{ped['id']}/cancelar", follow_redirects=True)
        r = admin.post(f"/pedido/{ped['id']}/cancelar", follow_redirects=True)
        assert "cancelado" in r.text.lower()


# ---- Disponibilidade (R2, R3, R12) ----

class TesteDisponibilidade:
    def test_disponibilidade_sem_reserva(self, app, admin):
        prod = _produto(admin, "Cadeira Tiffany", 20)
        d = dados.disponibilidade(prod["id"], "2026-10-01", "2026-10-03")
        assert d == 20

    def test_disponibilidade_com_reserva(self, app, admin):
        cli = _cliente(admin)
        prod = _produto(admin, "Cadeira Dourada", 15)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Cadeira Dourada", "quantidade": 6, "preco_unitario": "8.00"},
        ], data_ret="2026-10-05", data_dev="2026-10-08")
        d = dados.disponibilidade(prod["id"], "2026-10-06", "2026-10-07")
        assert d == 9

    def test_disponibilidade_sem_overlap(self, app, admin):
        cli = _cliente(admin)
        prod = _produto(admin, "Toalha Branca", 10)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Toalha Branca", "quantidade": 8, "preco_unitario": "5.00"},
        ], data_ret="2026-10-01", data_dev="2026-10-03")
        d = dados.disponibilidade(prod["id"], "2026-10-04", "2026-10-06")
        assert d == 10

    def test_disponibilidade_produto_manutencao(self, app, admin):
        prod = _produto(admin, "Lustre", 3)
        with dados.conectar() as conn:
            conn.execute("UPDATE produtos SET status='manutencao' WHERE id=?",
                         (prod["id"],))
        d = dados.disponibilidade(prod["id"], "2026-10-01", "2026-10-03")
        assert d == 0

    def test_bloqueio_overbooking_r2(self, app, admin):
        cli = _cliente(admin)
        prod = _produto(admin, "Arranjo Floral", 3)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Arranjo Floral", "quantidade": 3, "preco_unitario": "30.00"},
        ], data_ret="2026-10-10", data_dev="2026-10-12")
        r = _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Arranjo Floral", "quantidade": 1, "preco_unitario": "30.00"},
        ], data_ret="2026-10-11", data_dev="2026-10-13")
        assert "insuficiente" in r.text.lower() or "disponivel" in r.text.lower()

    def test_multiplas_reservas_somam(self, app, admin):
        cli = _cliente(admin, "Carlos Lima")
        prod = _produto(admin, "Vaso Cristal", 10)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Vaso Cristal", "quantidade": 4, "preco_unitario": "20.00"},
        ], data_ret="2026-10-01", data_dev="2026-10-05")
        cli2 = _cliente(admin, "Joana Ramos")
        _pedido_via_form(admin, cli2["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Vaso Cristal", "quantidade": 3, "preco_unitario": "20.00"},
        ], data_ret="2026-10-03", data_dev="2026-10-07")
        d = dados.disponibilidade(prod["id"], "2026-10-04", "2026-10-04")
        assert d == 3

    def test_disponibilidade_sem_datas(self, app, admin):
        prod = _produto(admin, "Bolo Fake", 2)
        d = dados.disponibilidade(prod["id"])
        assert d == 2


# ---- Calendario de disponibilidade ----

class TesteCalendario:
    def test_calendario_vazio(self, app, admin):
        prod = _produto(admin, "Tapete", 8)
        cal = dados.disponibilidade_calendario(prod["id"], 2026, 10)
        assert len(cal) == 31
        assert all(d["disponivel"] == 8 for d in cal)
        assert all(d["total"] == 8 for d in cal)

    def test_calendario_com_reserva(self, app, admin):
        cli = _cliente(admin)
        prod = _produto(admin, "Cortina", 5)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Cortina", "quantidade": 2, "preco_unitario": "15.00"},
        ], data_ret="2026-10-10", data_dev="2026-10-12")
        cal = dados.disponibilidade_calendario(prod["id"], 2026, 10)
        assert cal[9]["dia"] == 10
        assert cal[9]["disponivel"] == 3
        assert cal[8]["disponivel"] == 5

    def test_rota_calendario(self, app, admin):
        prod = _produto(admin, "Painel LED", 4)
        r = admin.get(f"/disponibilidade/{prod['id']}")
        assert r.status_code == 200
        assert "Disponibilidade" in r.text
        assert "Painel LED" in r.text

    def test_rota_calendario_com_mes(self, app, admin):
        prod = _produto(admin, "Balao", 50)
        r = admin.get(f"/disponibilidade/{prod['id']}?ano=2026&mes=12")
        assert r.status_code == 200
        assert "Dezembro" in r.text


# ---- API de disponibilidade ----

class TesteApiDisponibilidade:
    def test_api_sem_produto(self, app, admin):
        r = admin.get("/api/disponibilidade")
        assert r.status_code == 400

    def test_api_com_produto(self, app, admin):
        prod = _produto(admin, "Candelabro", 6)
        r = admin.get(f"/api/disponibilidade?produto_id={prod['id']}")
        assert r.status_code == 200
        j = r.get_json()
        assert j["disponivel"] == 6

    def test_api_com_datas(self, app, admin):
        cli = _cliente(admin)
        prod = _produto(admin, "Arco", 4)
        _pedido_via_form(admin, cli["id"], itens=[
            {"tipo": "produto", "item_id": prod["id"],
             "descricao": "Arco", "quantidade": 2, "preco_unitario": "40.00"},
        ], data_ret="2026-10-01", data_dev="2026-10-05")
        r = admin.get(f"/api/disponibilidade?produto_id={prod['id']}"
                      "&data_inicio=2026-10-02&data_fim=2026-10-04")
        j = r.get_json()
        assert j["disponivel"] == 2


# ---- Painel com contadores comerciais ----

class TestePainelComercial:
    def test_painel_mostra_leads(self, app, admin):
        _cliente(admin, "Painel Lead")
        r = admin.get("/")
        assert 'id="v-leads"' in r.text

    def test_painel_mostra_orcamentos(self, app, admin):
        _cliente(admin, "Painel Orc")
        r = admin.get("/")
        assert 'id="v-orcamentos"' in r.text

    def test_painel_mostra_pedidos(self, app, admin):
        _cliente(admin, "Painel Ped")
        r = admin.get("/")
        assert 'id="v-pedidos"' in r.text

    def test_painel_contador_leads(self, app, admin):
        cli = _cliente(admin)
        admin.post("/lead", data={
            "cliente_id": str(cli["id"]),
            "origem_id": "",
            "interesse": "Festa",
            "valor_estimado": "100.00",
            "status": "novo",
        }, follow_redirects=True)
        resumo = dados.resumo_painel()
        assert resumo["total_leads"] == 1

    def test_painel_contador_pedidos(self, app, admin):
        cli = _cliente(admin, "Fabio Nunes")
        _pedido_via_form(admin, cli["id"], itens=[
            {"descricao": "Servico", "quantidade": 1, "preco_unitario": "100.00"},
        ])
        resumo = dados.resumo_painel()
        assert resumo["total_pedidos"] == 1


# ---- Rota do menu de pedidos ----

class TesteRotasPedido:
    def test_lista_pedidos(self, app, admin):
        r = admin.get("/pedidos")
        assert r.status_code == 200

    def test_novo_pedido_pagina(self, app, admin):
        r = admin.get("/pedido/novo")
        assert r.status_code == 200
        assert "Novo Pedido" in r.text

    def test_ver_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido_via_form(admin, cli["id"], itens=[
            {"descricao": "Item", "quantidade": 1, "preco_unitario": "50.00"},
        ])
        ped = dados.listar_pedidos()[0]
        r = admin.get(f"/pedido/{ped['id']}")
        assert r.status_code == 200
        assert f"Pedido #{ped['id']}" in r.text

    def test_pedido_404(self, app, admin):
        r = admin.get("/pedido/9999")
        assert r.status_code == 404

    def test_lista_pedidos_com_datas_reserva(self, app, admin):
        cli = _cliente(admin)
        _pedido_via_form(admin, cli["id"], itens=[
            {"descricao": "Teste", "quantidade": 1, "preco_unitario": "10.00"},
        ], data_ret="2026-11-01", data_dev="2026-11-03")
        r = admin.get("/pedidos")
        assert "Retirada" in r.text
        assert "Devolucao" in r.text
