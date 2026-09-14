"""Testes do modulo de kits — Sprint 04."""

from sistema import dados


def _criar_produto(admin, nome="Mesa redonda", preco="50.00", qtd="10"):
    admin.post("/produto", data={
        "nome": nome,
        "quantidade_total": qtd,
        "preco_locacao": preco,
    }, follow_redirects=True)
    return [p for p in dados.listar_produtos() if p["nome"] == nome][0]


class TesteCriarKit:
    def test_cadastrar_kit_basico(self, app, admin):
        r = admin.post("/kit", data={
            "nome": "Kit Festa Infantil",
            "preco": "200.00",
        }, follow_redirects=True)
        assert r.status_code == 200
        kits = dados.listar_kits()
        assert any(k["nome"] == "Kit Festa Infantil" for k in kits)

    def test_nome_obrigatorio(self, app, admin):
        r = admin.post("/kit", data={
            "nome": "",
            "preco": "100.00",
        }, follow_redirects=True)
        assert "obrigatorio" in r.text.lower()

    def test_preco_independente_dos_produtos(self, app, admin):
        p = _criar_produto(admin, "Cadeira", "30.00")
        admin.post("/kit", data={
            "nome": "Kit Basico",
            "preco": "150.00",
        }, follow_redirects=True)
        kit = [k for k in dados.listar_kits() if k["nome"] == "Kit Basico"][0]
        assert kit["preco"] == 150.0

    def test_status_padrao_ativo(self, app, admin):
        admin.post("/kit", data={
            "nome": "Kit Padrao",
            "preco": "100.00",
        }, follow_redirects=True)
        kit = [k for k in dados.listar_kits() if k["nome"] == "Kit Padrao"][0]
        assert kit["status"] == "ativo"


class TesteEditarKit:
    def test_alterar_nome_e_preco(self, app, admin):
        admin.post("/kit", data={"nome": "Kit A", "preco": "100.00"},
                    follow_redirects=True)
        kit = dados.listar_kits()[0]
        r = admin.post(f"/kit/{kit['id']}", data={
            "nome": "Kit A Deluxe",
            "preco": "250.00",
            "status": "ativo",
        }, follow_redirects=True)
        assert r.status_code == 200
        atualizado = dados.buscar_kit(kit["id"])
        assert atualizado["nome"] == "Kit A Deluxe"
        assert atualizado["preco"] == 250.0

    def test_alterar_status_para_inativo(self, app, admin):
        admin.post("/kit", data={"nome": "Kit Temp", "preco": "80.00"},
                    follow_redirects=True)
        kit = dados.listar_kits()[0]
        admin.post(f"/kit/{kit['id']}", data={
            "nome": "Kit Temp",
            "preco": "80.00",
            "status": "inativo",
        }, follow_redirects=True)
        atualizado = dados.buscar_kit(kit["id"])
        assert atualizado["status"] == "inativo"

    def test_kit_inexistente_retorna_404(self, app, admin):
        r = admin.get("/kit/9999")
        assert r.status_code == 404


class TesteItensKit:
    def test_adicionar_produto_ao_kit(self, app, admin):
        p = _criar_produto(admin)
        admin.post("/kit", data={"nome": "Kit C", "preco": "200.00"},
                    follow_redirects=True)
        kit = dados.listar_kits()[0]

        r = admin.post(f"/kit/{kit['id']}/item", data={
            "produto_id": str(p["id"]),
            "quantidade": "3",
        }, follow_redirects=True)
        assert r.status_code == 200

        atualizado = dados.buscar_kit(kit["id"])
        assert len(atualizado["itens"]) == 1
        assert atualizado["itens"][0]["quantidade"] == 3

    def test_produto_duplicado_atualiza_quantidade(self, app, admin):
        p = _criar_produto(admin)
        admin.post("/kit", data={"nome": "Kit D", "preco": "100.00"},
                    follow_redirects=True)
        kit = dados.listar_kits()[0]

        dados.adicionar_item_kit(kit["id"], p["id"], 2)
        dados.adicionar_item_kit(kit["id"], p["id"], 5)

        atualizado = dados.buscar_kit(kit["id"])
        assert len(atualizado["itens"]) == 1
        assert atualizado["itens"][0]["quantidade"] == 5

    def test_remover_item_do_kit(self, app, admin):
        p = _criar_produto(admin)
        admin.post("/kit", data={"nome": "Kit E", "preco": "100.00"},
                    follow_redirects=True)
        kit = dados.listar_kits()[0]

        item_id = dados.adicionar_item_kit(kit["id"], p["id"], 1)
        admin.post(f"/kit/{kit['id']}/item/{item_id}/remover",
                    follow_redirects=True)

        atualizado = dados.buscar_kit(kit["id"])
        assert len(atualizado["itens"]) == 0

    def test_quantidade_minima_1(self, app, admin):
        p = _criar_produto(admin)
        admin.post("/kit", data={"nome": "Kit F", "preco": "50.00"},
                    follow_redirects=True)
        kit = dados.listar_kits()[0]

        try:
            dados.adicionar_item_kit(kit["id"], p["id"], 0)
            assert False, "Deveria lancar ErroDeCampo"
        except dados.ErroDeCampo:
            pass

    def test_soma_produtos_calculada(self, app, admin):
        p1 = _criar_produto(admin, "Mesa", "50.00")
        p2 = _criar_produto(admin, "Cadeira", "20.00")
        admin.post("/kit", data={"nome": "Kit G", "preco": "120.00"},
                    follow_redirects=True)
        kit = dados.listar_kits()[0]

        dados.adicionar_item_kit(kit["id"], p1["id"], 2)
        dados.adicionar_item_kit(kit["id"], p2["id"], 4)

        atualizado = dados.buscar_kit(kit["id"])
        assert atualizado["soma_produtos"] == 2 * 50.0 + 4 * 20.0


class TesteDisponibilidadeKit:
    def test_disponibilidade_minimo_dos_produtos(self, app, admin):
        p1 = _criar_produto(admin, "Toalha", "10.00", "20")
        p2 = _criar_produto(admin, "Guardanapo", "5.00", "50")
        kit_id = dados.salvar_kit({"nome": "Kit Mesa", "preco": 80})
        dados.adicionar_item_kit(kit_id, p1["id"], 4)
        dados.adicionar_item_kit(kit_id, p2["id"], 10)

        disp = dados.disponibilidade_kit(kit_id)
        assert disp == min(20 // 4, 50 // 10)
        assert disp == 5

    def test_kit_vazio_disponibilidade_zero(self, app, admin):
        kit_id = dados.salvar_kit({"nome": "Kit Vazio", "preco": 50})
        assert dados.disponibilidade_kit(kit_id) == 0

    def test_produto_em_manutencao_zera_kit(self, app, admin):
        p1 = _criar_produto(admin, "Arco", "30.00", "10")
        kit_id = dados.salvar_kit({"nome": "Kit Arco", "preco": 60})
        dados.adicionar_item_kit(kit_id, p1["id"], 1)

        assert dados.disponibilidade_kit(kit_id) == 10

        with dados.conectar() as conn:
            conn.execute("UPDATE produtos SET status='manutencao' WHERE id=?",
                         (p1["id"],))

        assert dados.disponibilidade_kit(kit_id) == 0

    def test_produto_inativo_zera_kit(self, app, admin):
        p1 = _criar_produto(admin, "Balao", "5.00", "100")
        kit_id = dados.salvar_kit({"nome": "Kit Balao", "preco": 30})
        dados.adicionar_item_kit(kit_id, p1["id"], 5)

        with dados.conectar() as conn:
            conn.execute("UPDATE produtos SET status='inativo' WHERE id=?",
                         (p1["id"],))

        assert dados.disponibilidade_kit(kit_id) == 0


class TesteListaKits:
    def test_pagina_lista_kits(self, app, admin):
        dados.salvar_kit({"nome": "Kit L1", "preco": 100})
        dados.salvar_kit({"nome": "Kit L2", "preco": 200})
        r = admin.get("/kits")
        assert r.status_code == 200
        assert "Kit L1" in r.text
        assert "Kit L2" in r.text

    def test_filtro_ativos(self, app, admin):
        dados.salvar_kit({"nome": "Kit Ativo", "preco": 100, "status": "ativo"})
        dados.salvar_kit({"nome": "Kit Inativo", "preco": 100, "status": "inativo"})
        r = admin.get("/kits")
        assert "Kit Ativo" in r.text
        assert "Kit Inativo" not in r.text

    def test_filtro_inativos(self, app, admin):
        dados.salvar_kit({"nome": "Kit A2", "preco": 100, "status": "ativo"})
        dados.salvar_kit({"nome": "Kit I2", "preco": 100, "status": "inativo"})
        r = admin.get("/kits?ver=inativos")
        assert "Kit I2" in r.text
        assert "Kit A2" not in r.text

    def test_filtro_todos(self, app, admin):
        dados.salvar_kit({"nome": "Kit A3", "preco": 100, "status": "ativo"})
        dados.salvar_kit({"nome": "Kit I3", "preco": 100, "status": "inativo"})
        r = admin.get("/kits?ver=todos")
        assert "Kit A3" in r.text
        assert "Kit I3" in r.text

    def test_busca_por_nome(self, app, admin):
        dados.salvar_kit({"nome": "Kit Princesa", "preco": 300})
        dados.salvar_kit({"nome": "Kit Safari", "preco": 250})
        r = admin.get("/kits?q=Princesa")
        assert "Kit Princesa" in r.text
        assert "Kit Safari" not in r.text

    def test_economia_exibida(self, app, admin):
        p = _criar_produto(admin, "Item caro", "100.00")
        kit_id = dados.salvar_kit({"nome": "Kit Eco", "preco": 150})
        dados.adicionar_item_kit(kit_id, p["id"], 2)

        r = admin.get("/kits")
        assert "-25%" in r.text

    def test_contagem_itens_na_lista(self, app, admin):
        p = _criar_produto(admin, "Peca X", "10.00")
        kit_id = dados.salvar_kit({"nome": "Kit Count", "preco": 50})
        dados.adicionar_item_kit(kit_id, p["id"], 3)
        r = admin.get("/kits")
        assert r.status_code == 200


class TestePerfilKits:
    def test_admin_pode_criar_kit(self, app, admin):
        r = admin.get("/kit")
        assert r.status_code == 200

    def test_login_necessario_para_listar(self, app, client):
        r = client.get("/kits")
        assert r.status_code == 302

    def test_nao_admin_nao_cria(self, app, admin):
        admin.post("/usuario", data={
            "nome": "Operador",
            "login": "oper1",
            "senha": "senhaoper1",
            "perfil": "operacional",
            "ativo": "1",
        }, follow_redirects=True)
        admin.get("/sair")
        admin.post("/entrar", data={"login": "oper1", "senha": "senhaoper1"})
        r = admin.get("/kit")
        assert r.status_code == 302 or r.status_code == 403


class TesteAuditKit:
    def test_criacao_registra_auditoria(self, app, admin):
        admin.post("/kit", data={
            "nome": "Kit Audit",
            "preco": "100.00",
        }, follow_redirects=True)
        acoes = dados.listar_audit(limite=5)
        kit_acao = [a for a in acoes if "kit" in a.get("tipo", "")
                    and "criou" in a.get("tipo", "")]
        assert len(kit_acao) >= 1


class TestePainelKits:
    def test_painel_mostra_total_kits(self, app, admin):
        dados.salvar_kit({"nome": "Kit P1", "preco": 100})
        dados.salvar_kit({"nome": "Kit P2", "preco": 200})
        r = admin.get("/")
        assert r.status_code == 200
        assert 'id="v-kits"' in r.text

    def test_resumo_painel_inclui_kits(self, app, admin):
        dados.salvar_kit({"nome": "Kit R1", "preco": 50})
        resumo = dados.resumo_painel()
        assert "total_kits" in resumo
        assert resumo["total_kits"] >= 1


class TesteCartoesKits:
    def test_data_rotulo_bate_com_cabecalho(self, app, admin):
        dados.salvar_kit({"nome": "Kit Cartao", "preco": 100})
        r = admin.get("/kits")
        html = r.text

        import re
        ths = re.findall(r'<th[^>]*>(.*?)</th>', html)
        rotulos = re.findall(r'data-rotulo="([^"]*)"', html)

        ths_com_texto = [t.strip() for t in ths if t.strip()]
        for rotulo in rotulos:
            assert rotulo in ths_com_texto, \
                f"data-rotulo=\"{rotulo}\" nao bate com nenhum <th>"
