"""Testes do modulo de produtos — Sprint 03."""

from sistema import dados


class TesteCriarProduto:
    def test_cadastrar_produto_basico(self, app, admin):
        r = admin.post("/produto", data={
            "nome": "Mesa redonda 1.20m",
            "quantidade_total": "10",
            "preco_locacao": "25.00",
        }, follow_redirects=True)
        assert r.status_code == 200
        prods = dados.listar_produtos()
        assert any(p["nome"] == "Mesa redonda 1.20m" for p in prods)

    def test_nome_obrigatorio(self, app, admin):
        r = admin.post("/produto", data={
            "nome": "",
            "quantidade_total": "5",
        }, follow_redirects=True)
        assert "obrigatorio" in r.text

    def test_sku_gerado_automaticamente(self, app, admin):
        admin.post("/produto", data={
            "nome": "Cadeira plastica",
            "quantidade_total": "20",
        }, follow_redirects=True)
        prods = dados.listar_produtos()
        p = [x for x in prods if x["nome"] == "Cadeira plastica"][0]
        assert p["codigo_sku"]
        assert "-" in p["codigo_sku"]

    def test_todos_os_campos(self, app, admin):
        dados.salvar_categoria("Infantil")
        cats = dados.listar_categorias()
        cat_id = cats[0]["id"]

        r = admin.post("/produto", data={
            "nome": "Piscina de bolinhas",
            "categoria_id": str(cat_id),
            "descricao": "Piscina grande com 500 bolinhas",
            "preco_locacao": "80.00",
            "valor_referencia": "350.00",
            "quantidade_total": "3",
            "status": "disponivel",
            "localizacao": "Galpao B2",
            "observacoes": "Limpar bolinhas apos uso",
            "tags": "infantil, piscina, destaque",
        }, follow_redirects=True)
        assert r.status_code == 200
        prods = dados.listar_produtos()
        p = [x for x in prods if x["nome"] == "Piscina de bolinhas"][0]
        assert p["categoria_id"] == cat_id
        assert p["preco_locacao"] == 80.0
        assert "infantil" in p["tags"]
        assert "piscina" in p["tags"]


class TesteCategorias:
    def test_criar_categoria(self, app, admin):
        r = admin.post("/categoria", data={
            "nome": "Mesas",
        }, follow_redirects=True)
        assert r.status_code == 200
        cats = dados.listar_categorias()
        assert any(c["nome"] == "Mesas" for c in cats)

    def test_criar_subcategoria(self, app, admin):
        pai_id = dados.salvar_categoria("Estruturas")
        r = admin.post("/categoria", data={
            "nome": "Tendas",
            "pai_id": str(pai_id),
        }, follow_redirects=True)
        assert r.status_code == 200
        arvore = dados.categorias_arvore()
        est = [c for c in arvore if c["nome"] == "Estruturas"][0]
        assert any(f["nome"] == "Tendas" for f in est["filhos"])

    def test_nome_obrigatorio_categoria(self, app, admin):
        r = admin.post("/categoria", data={
            "nome": "",
        }, follow_redirects=True)
        assert "obrigatorio" in r.text.lower()

    def test_excluir_categoria_vazia(self, app, admin):
        cat_id = dados.salvar_categoria("Para excluir")
        r = admin.post(f"/categoria/{cat_id}/excluir",
                       follow_redirects=True)
        assert r.status_code == 200
        assert not dados.buscar_categoria(cat_id)

    def test_excluir_categoria_em_uso(self, app, admin):
        cat_id = dados.salvar_categoria("Em uso")
        dados.salvar_produto({"nome": "Prod", "categoria_id": cat_id,
                              "status": "disponivel"})
        r = admin.post(f"/categoria/{cat_id}/excluir",
                       follow_redirects=True)
        assert "em uso" in r.text.lower()
        assert dados.buscar_categoria(cat_id)

    def test_listar_categorias_rota(self, app, admin):
        dados.salvar_categoria("Acessorios")
        r = admin.get("/categorias")
        assert r.status_code == 200
        assert "Acessorios" in r.text


class TesteListaProdutos:
    def _popular(self):
        dados.salvar_categoria("Mesas")
        cats = dados.listar_categorias()
        cat_id = cats[0]["id"]
        dados.salvar_produto(
            {"nome": "Mesa redonda", "categoria_id": cat_id,
             "preco_locacao": 25, "quantidade_total": 10,
             "status": "disponivel"}, tags=["destaque"])
        dados.salvar_produto(
            {"nome": "Cadeira ferro", "preco_locacao": 5,
             "quantidade_total": 50, "status": "disponivel"})
        dados.salvar_produto(
            {"nome": "Tenda avariada", "preco_locacao": 100,
             "quantidade_total": 2, "status": "manutencao"})
        dados.salvar_produto(
            {"nome": "Item velho", "preco_locacao": 10,
             "quantidade_total": 1, "status": "inativo"})

    def test_lista_mostra_disponiveis(self, app, admin):
        self._popular()
        r = admin.get("/produtos")
        assert "Mesa redonda" in r.text
        assert "Cadeira ferro" in r.text
        assert "Tenda avariada" not in r.text
        assert "Item velho" not in r.text

    def test_lista_manutencao(self, app, admin):
        self._popular()
        r = admin.get("/produtos?ver=manutencao")
        assert "Tenda avariada" in r.text
        assert "Mesa redonda" not in r.text

    def test_lista_inativos(self, app, admin):
        self._popular()
        r = admin.get("/produtos?ver=inativos")
        assert "Item velho" in r.text
        assert "Mesa redonda" not in r.text

    def test_lista_todos(self, app, admin):
        self._popular()
        r = admin.get("/produtos?ver=todos")
        assert "Mesa redonda" in r.text
        assert "Tenda avariada" in r.text
        assert "Item velho" in r.text

    def test_busca_por_nome(self, app, admin):
        self._popular()
        r = admin.get("/produtos?q=redonda")
        assert "Mesa redonda" in r.text
        assert "Cadeira ferro" not in r.text

    def test_filtro_por_categoria(self, app, admin):
        self._popular()
        cats = dados.listar_categorias()
        cat_id = cats[0]["id"]
        r = admin.get(f"/produtos?categoria={cat_id}")
        assert "Mesa redonda" in r.text
        assert "Cadeira ferro" not in r.text

    def test_filtro_por_tag(self, app, admin):
        self._popular()
        r = admin.get("/produtos?tag=destaque")
        assert "Mesa redonda" in r.text
        assert "Cadeira ferro" not in r.text


class TesteEditarProduto:
    def test_editar_produto(self, app, admin):
        pid = dados.salvar_produto(
            {"nome": "Original", "status": "disponivel",
             "quantidade_total": 5})
        r = admin.post(f"/produto/{pid}", data={
            "nome": "Editado",
            "quantidade_total": "8",
            "preco_locacao": "30.00",
        }, follow_redirects=True)
        assert r.status_code == 200
        p = dados.buscar_produto(pid)
        assert p["nome"] == "Editado"
        assert p["quantidade_total"] == 8

    def test_inativar_produto(self, app, admin):
        pid = dados.salvar_produto(
            {"nome": "Para inativar", "status": "disponivel"})
        admin.post(f"/produto/{pid}", data={
            "nome": "Para inativar",
            "status": "inativo",
        }, follow_redirects=True)
        p = dados.buscar_produto(pid)
        assert p["status"] == "inativo"

    def test_produto_inexistente_404(self, app, admin):
        r = admin.get("/produto/9999")
        assert r.status_code == 404


class TesteTagsProduto:
    def test_criar_com_tags(self, app):
        pid = dados.salvar_produto(
            {"nome": "Com tags", "status": "disponivel"},
            tags=["destaque", "infantil"])
        p = dados.buscar_produto(pid)
        assert "destaque" in p["tags"]
        assert "infantil" in p["tags"]

    def test_atualizar_tags(self, app):
        pid = dados.salvar_produto(
            {"nome": "Tags", "status": "disponivel"},
            tags=["VIP"])
        dados.salvar_produto(
            {"nome": "Tags", "status": "disponivel"},
            id_=pid, tags=["novo"])
        p = dados.buscar_produto(pid)
        assert "novo" in p["tags"]
        assert "VIP" not in p["tags"]


class TesteSKU:
    def test_sku_com_prefixo_categoria(self, app):
        cat_id = dados.salvar_categoria("Infantil")
        pid = dados.salvar_produto(
            {"nome": "Piscina", "categoria_id": cat_id,
             "status": "disponivel"})
        p = dados.buscar_produto(pid)
        assert p["codigo_sku"].startswith("INF-")

    def test_sku_sem_categoria(self, app):
        pid = dados.salvar_produto(
            {"nome": "Generico", "status": "disponivel"})
        p = dados.buscar_produto(pid)
        assert p["codigo_sku"].startswith("GER-")

    def test_sku_sequencial(self, app):
        pid1 = dados.salvar_produto(
            {"nome": "A", "status": "disponivel"})
        pid2 = dados.salvar_produto(
            {"nome": "B", "status": "disponivel"})
        p1 = dados.buscar_produto(pid1)
        p2 = dados.buscar_produto(pid2)
        assert p1["codigo_sku"] == "GER-0001"
        assert p2["codigo_sku"] == "GER-0002"

    def test_sku_preservado_na_edicao(self, app):
        pid = dados.salvar_produto(
            {"nome": "Original", "status": "disponivel"})
        p1 = dados.buscar_produto(pid)
        sku_original = p1["codigo_sku"]
        dados.salvar_produto(
            {"nome": "Editado", "status": "disponivel"}, id_=pid)
        p2 = dados.buscar_produto(pid)
        assert p2["codigo_sku"] == sku_original


class TesteDisponibilidade:
    def test_disponibilidade_basica(self, app):
        pid = dados.salvar_produto(
            {"nome": "Mesa", "status": "disponivel",
             "quantidade_total": 10})
        disp = dados.disponibilidade(pid)
        assert disp == 10

    def test_disponibilidade_produto_inexistente(self, app):
        assert dados.disponibilidade(9999) == 0


class TesteFotosProduto:
    def test_salvar_foto_primeira_vira_capa(self, app):
        pid = dados.salvar_produto(
            {"nome": "Com foto", "status": "disponivel"})
        fid = dados.salvar_foto_produto(pid, "uploads/produtos/1/foto.jpg")
        p = dados.buscar_produto(pid)
        assert len(p["fotos"]) == 1
        assert p["fotos"][0]["principal"] == 1

    def test_segunda_foto_nao_capa(self, app):
        pid = dados.salvar_produto(
            {"nome": "Com fotos", "status": "disponivel"})
        dados.salvar_foto_produto(pid, "uploads/produtos/1/foto1.jpg")
        dados.salvar_foto_produto(pid, "uploads/produtos/1/foto2.jpg")
        p = dados.buscar_produto(pid)
        assert len(p["fotos"]) == 2
        capas = [f for f in p["fotos"] if f["principal"]]
        assert len(capas) == 1

    def test_definir_capa(self, app):
        pid = dados.salvar_produto(
            {"nome": "Trocar capa", "status": "disponivel"})
        f1 = dados.salvar_foto_produto(pid, "uploads/produtos/1/a.jpg")
        f2 = dados.salvar_foto_produto(pid, "uploads/produtos/1/b.jpg")
        dados.definir_foto_principal(f2, pid)
        p = dados.buscar_produto(pid)
        for f in p["fotos"]:
            if f["id"] == f2:
                assert f["principal"] == 1
            else:
                assert f["principal"] == 0

    def test_excluir_foto_promove_outra(self, app):
        pid = dados.salvar_produto(
            {"nome": "Excluir foto", "status": "disponivel"})
        f1 = dados.salvar_foto_produto(pid, "uploads/produtos/1/a.jpg")
        f2 = dados.salvar_foto_produto(pid, "uploads/produtos/1/b.jpg")
        dados.excluir_foto_produto(f1)
        p = dados.buscar_produto(pid)
        assert len(p["fotos"]) == 1
        assert p["fotos"][0]["principal"] == 1


class TestePerfilProdutos:
    def test_todos_acessam_lista(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Operador", "login": "oper", "perfil": "operacional"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "oper", "senha": "123"})
        r = client.get("/produtos")
        assert r.status_code == 200

    def test_operacional_nao_cria_produto(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Operador", "login": "oper2", "perfil": "operacional"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "oper2", "senha": "123"})
        r = client.get("/produto")
        assert r.status_code == 403

    def test_comercial_nao_cria_produto(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Vendedor", "login": "vend", "perfil": "comercial"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "vend", "senha": "123"})
        r = client.get("/produto")
        assert r.status_code == 403


class TesteAuditProduto:
    def test_criar_grava_audit(self, app, admin):
        admin.post("/produto", data={"nome": "Audit Teste"},
                   follow_redirects=True)
        logs = dados.listar_audit()
        assert any("produto" in (l.get("tipo") or "") for l in logs)


class TestePainelProdutos:
    def test_painel_mostra_total_produtos(self, app, admin):
        dados.salvar_produto(
            {"nome": "Prod 1", "status": "disponivel"})
        dados.salvar_produto(
            {"nome": "Prod 2", "status": "disponivel"})
        r = admin.get("/")
        assert r.status_code == 200
        assert "v-produtos" in r.text

    def test_painel_nao_conta_inativos(self, app, admin):
        dados.salvar_produto(
            {"nome": "Ativo", "status": "disponivel"})
        dados.salvar_produto(
            {"nome": "Inativo", "status": "inativo"})
        resumo = dados.resumo_painel()
        assert resumo["total_produtos"] == 1


class TesteCartoesProdutos:
    def _checar_rotulos(self, html):
        import re
        ths = re.findall(r'<th[^>]*>(.*?)</th>', html)
        ths = [t.strip() for t in ths if t.strip()]
        rotulos = re.findall(r'data-rotulo="([^"]*)"', html)
        for rotulo in rotulos:
            assert rotulo in ths, f"data-rotulo='{rotulo}' nao encontrado nos cabecalhos {ths}"

    def test_tabela_produtos(self, app, admin):
        dados.salvar_produto(
            {"nome": "Rotulo Teste", "status": "disponivel"})
        r = admin.get("/produtos")
        self._checar_rotulos(r.text)
