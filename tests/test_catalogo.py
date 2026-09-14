"""Testes do catalogo publico e interno — Sprint 05."""

from sistema import dados


def _produto(admin, nome="Mesa redonda", preco="50.00", qtd="10",
             publicado=False, status="disponivel"):
    admin.post("/produto", data={
        "nome": nome,
        "quantidade_total": qtd,
        "preco_locacao": preco,
        "status": status,
        "publicado": "1" if publicado else "",
    }, follow_redirects=True)
    return [p for p in dados.listar_produtos() if p["nome"] == nome][0]


def _kit(admin, nome="Kit Festa", preco="200.00",
         publicado=False, status="ativo"):
    admin.post("/kit", data={
        "nome": nome,
        "preco": preco,
        "status": status,
        "publicado": "1" if publicado else "",
    }, follow_redirects=True)
    return [k for k in dados.listar_kits() if k["nome"] == nome][0]


# ---- Catalogo publico: acesso sem login ----

class TesteCatalogoPublicoAcesso:
    def test_catalogo_abre_sem_login(self, app, client):
        r = client.get("/catalogo")
        assert r.status_code == 200

    def test_catalogo_vazio_mostra_mensagem(self, app, client):
        r = client.get("/catalogo")
        assert "em breve" in r.text.lower()

    def test_catalogo_produto_nao_publicado_404(self, app, admin, client):
        p = _produto(admin, publicado=False)
        r = client.get(f"/catalogo/produto/{p['id']}")
        assert r.status_code == 404

    def test_catalogo_kit_nao_publicado_404(self, app, admin, client):
        k = _kit(admin, publicado=False)
        r = client.get(f"/catalogo/kit/{k['id']}")
        assert r.status_code == 404


# ---- Filtro publicado ----

class TesteFiltroPublicado:
    def test_produto_publicado_aparece_no_catalogo(self, app, admin, client):
        _produto(admin, "Cadeira pub", publicado=True)
        r = client.get("/catalogo")
        assert "Cadeira pub" in r.text

    def test_produto_nao_publicado_nao_aparece(self, app, admin, client):
        _produto(admin, "Cadeira oculta", publicado=False)
        r = client.get("/catalogo")
        assert "Cadeira oculta" not in r.text

    def test_kit_publicado_aparece_no_catalogo(self, app, admin, client):
        _kit(admin, "Kit Visivel", publicado=True)
        r = client.get("/catalogo")
        assert "Kit Visivel" in r.text

    def test_kit_nao_publicado_nao_aparece(self, app, admin, client):
        _kit(admin, "Kit Oculto", publicado=False)
        r = client.get("/catalogo")
        assert "Kit Oculto" not in r.text

    def test_produto_indisponivel_nao_aparece(self, app, admin, client):
        _produto(admin, "Cadeira quebrada", publicado=True,
                 status="manutencao")
        r = client.get("/catalogo")
        assert "Cadeira quebrada" not in r.text

    def test_kit_inativo_nao_aparece(self, app, admin, client):
        _kit(admin, "Kit Velho", publicado=True, status="inativo")
        r = client.get("/catalogo")
        assert "Kit Velho" not in r.text


# ---- Detalhe publico ----

class TesteDetalheProduto:
    def test_detalhe_carrega(self, app, admin, client):
        p = _produto(admin, "Toalha bordada", publicado=True)
        r = client.get(f"/catalogo/produto/{p['id']}")
        assert r.status_code == 200
        assert "Toalha bordada" in r.text

    def test_detalhe_tem_preco(self, app, admin, client):
        p = _produto(admin, "Toalha com preco", preco="75.00",
                     publicado=True)
        r = client.get(f"/catalogo/produto/{p['id']}")
        assert "75" in r.text

    def test_detalhe_tem_meta_og(self, app, admin, client):
        p = _produto(admin, "Toalha OG", publicado=True)
        r = client.get(f"/catalogo/produto/{p['id']}")
        assert 'og:title' in r.text
        assert 'og:type' in r.text

    def test_detalhe_tem_link_voltar(self, app, admin, client):
        p = _produto(admin, "Toalha voltar", publicado=True)
        r = client.get(f"/catalogo/produto/{p['id']}")
        assert "catalogo" in r.text.lower()

    def test_produto_inexistente_404(self, app, client):
        r = client.get("/catalogo/produto/9999")
        assert r.status_code == 404


class TesteDetalheKit:
    def test_detalhe_kit_carrega(self, app, admin, client):
        k = _kit(admin, "Kit Completo", publicado=True)
        r = client.get(f"/catalogo/kit/{k['id']}")
        assert r.status_code == 200
        assert "Kit Completo" in r.text

    def test_detalhe_kit_mostra_itens(self, app, admin, client):
        p = _produto(admin, "Cadeira kit", publicado=True)
        k = _kit(admin, "Kit com item", publicado=True)
        dados.adicionar_item_kit(k["id"], p["id"], 5)
        r = client.get(f"/catalogo/kit/{k['id']}")
        assert "Cadeira kit" in r.text
        assert "5" in r.text

    def test_detalhe_kit_economia(self, app, admin, client):
        p = _produto(admin, "Cadeira eco", preco="100.00", publicado=True)
        k = _kit(admin, "Kit Economico", preco="80.00", publicado=True)
        dados.adicionar_item_kit(k["id"], p["id"], 1)
        r = client.get(f"/catalogo/kit/{k['id']}")
        assert "economia" in r.text.lower()

    def test_detalhe_kit_meta_og(self, app, admin, client):
        k = _kit(admin, "Kit OG", publicado=True)
        r = client.get(f"/catalogo/kit/{k['id']}")
        assert 'og:title' in r.text

    def test_kit_inexistente_404(self, app, client):
        r = client.get("/catalogo/kit/9999")
        assert r.status_code == 404


# ---- Busca e filtro ----

class TesteBuscaFiltro:
    def test_busca_produto(self, app, admin, client):
        _produto(admin, "Arranjo floral", publicado=True)
        _produto(admin, "Mesa grande", publicado=True)
        r = client.get("/catalogo?q=floral")
        assert "Arranjo floral" in r.text
        assert "Mesa grande" not in r.text

    def test_filtro_categoria(self, app, admin, client):
        admin.post("/categoria", data={"nome": "Moveis"},
                   follow_redirects=True)
        cats = dados.listar_categorias()
        cat_id = [c for c in cats if c["nome"] == "Moveis"][0]["id"]
        admin.post("/produto", data={
            "nome": "Cadeira cat", "quantidade_total": "5",
            "preco_locacao": "30.00", "categoria_id": str(cat_id),
            "publicado": "1",
        }, follow_redirects=True)
        _produto(admin, "Toalha sem cat", publicado=True)
        r = client.get(f"/catalogo?categoria={cat_id}")
        assert "Cadeira cat" in r.text
        assert "Toalha sem cat" not in r.text

    def test_busca_sem_resultado(self, app, admin, client):
        _produto(admin, "Cadeira normal", publicado=True)
        r = client.get("/catalogo?q=inexistente")
        assert "Nenhum resultado" in r.text


# ---- WhatsApp ----

class TesteWhatsApp:
    def test_produto_tem_link_whatsapp(self, app, admin, client):
        dados.salvar_organizacao({"whatsapp": "5567999999999"})
        p = _produto(admin, "Cadeira WA", publicado=True)
        r = client.get(f"/catalogo/produto/{p['id']}")
        assert "wa.me" in r.text
        assert "Cadeira WA" in r.text

    def test_kit_tem_link_whatsapp(self, app, admin, client):
        dados.salvar_organizacao({"whatsapp": "5567999999999"})
        k = _kit(admin, "Kit WA", publicado=True)
        r = client.get(f"/catalogo/kit/{k['id']}")
        assert "wa.me" in r.text
        assert "Kit WA" in r.text


# ---- Catalogo interno ----

class TesteCatalogoInterno:
    def test_interno_exige_login(self, app, client):
        r = client.get("/catalogo-interno")
        assert r.status_code in (302, 303)

    def test_interno_carrega(self, app, admin):
        r = admin.get("/catalogo-interno")
        assert r.status_code == 200
        assert "Vitrine" in r.text

    def test_interno_mostra_badge_publicado(self, app, admin):
        _produto(admin, "Cadeira publica", publicado=True)
        _produto(admin, "Cadeira privada", publicado=False)
        r = admin.get("/catalogo-interno")
        assert "publicado" in r.text
        assert "nao publicado" in r.text

    def test_interno_mostra_todos_produtos(self, app, admin):
        _produto(admin, "Pub", publicado=True)
        _produto(admin, "Nao pub", publicado=False)
        r = admin.get("/catalogo-interno")
        assert "Pub" in r.text
        assert "Nao pub" in r.text

    def test_interno_link_para_catalogo_publico(self, app, admin):
        r = admin.get("/catalogo-interno")
        assert "catalogo publico" in r.text.lower() or "/catalogo" in r.text


# ---- Checkbox publicado no formulario ----

class TesteCheckboxPublicado:
    def test_produto_salva_publicado(self, app, admin):
        p = _produto(admin, "Cadeira check", publicado=True)
        prod = dados.buscar_produto(p["id"])
        assert prod["publicado"] == 1

    def test_produto_salva_nao_publicado(self, app, admin):
        p = _produto(admin, "Cadeira uncheck", publicado=False)
        prod = dados.buscar_produto(p["id"])
        assert prod["publicado"] == 0

    def test_kit_salva_publicado(self, app, admin):
        k = _kit(admin, "Kit check", publicado=True)
        kit = dados.buscar_kit(k["id"])
        assert kit["publicado"] == 1

    def test_kit_salva_nao_publicado(self, app, admin):
        k = _kit(admin, "Kit uncheck", publicado=False)
        kit = dados.buscar_kit(k["id"])
        assert kit["publicado"] == 0

    def test_checkbox_aparece_no_formulario_produto(self, app, admin):
        r = admin.get("/produto")
        assert "publicado" in r.text.lower()
        assert 'name="publicado"' in r.text

    def test_checkbox_aparece_no_formulario_kit(self, app, admin):
        r = admin.get("/kit")
        assert "publicado" in r.text.lower()
        assert 'name="publicado"' in r.text


# ---- Dados: funcoes do catalogo ----

class TesteDadosCatalogo:
    def test_catalogo_produtos_filtra_publicado(self, app, admin):
        _produto(admin, "Pub direto", publicado=True)
        _produto(admin, "Nao pub direto", publicado=False)
        prods = dados.catalogo_produtos()
        nomes = [p["nome"] for p in prods]
        assert "Pub direto" in nomes
        assert "Nao pub direto" not in nomes

    def test_catalogo_kits_filtra_publicado(self, app, admin):
        _kit(admin, "Kit pub direto", publicado=True)
        _kit(admin, "Kit nao pub direto", publicado=False)
        kits = dados.catalogo_kits()
        nomes = [k["nome"] for k in kits]
        assert "Kit pub direto" in nomes
        assert "Kit nao pub direto" not in nomes

    def test_produto_publico_retorna_publicado(self, app, admin):
        p = _produto(admin, "Visivel", publicado=True)
        resultado = dados.produto_publico(p["id"])
        assert resultado is not None
        assert resultado["nome"] == "Visivel"

    def test_produto_publico_retorna_none_se_nao_publicado(self, app, admin):
        p = _produto(admin, "Invisivel", publicado=False)
        assert dados.produto_publico(p["id"]) is None

    def test_kit_publico_retorna_publicado(self, app, admin):
        k = _kit(admin, "Kit vis", publicado=True)
        resultado = dados.kit_publico(k["id"])
        assert resultado is not None
        assert resultado["nome"] == "Kit vis"

    def test_kit_publico_retorna_none_se_nao_publicado(self, app, admin):
        k = _kit(admin, "Kit invis", publicado=False)
        assert dados.kit_publico(k["id"]) is None

    def test_catalogo_produtos_inclui_categoria(self, app, admin):
        admin.post("/categoria", data={"nome": "Tecidos"},
                   follow_redirects=True)
        cats = dados.listar_categorias()
        cat_id = [c for c in cats if c["nome"] == "Tecidos"][0]["id"]
        admin.post("/produto", data={
            "nome": "Toalha rosa", "quantidade_total": "5",
            "preco_locacao": "25.00", "categoria_id": str(cat_id),
            "publicado": "1",
        }, follow_redirects=True)
        prods = dados.catalogo_produtos()
        tp = [p for p in prods if p["nome"] == "Toalha rosa"][0]
        assert tp["categoria_nome"] == "Tecidos"

    def test_catalogo_produtos_filtra_por_categoria(self, app, admin):
        admin.post("/categoria", data={"nome": "Cat A"},
                   follow_redirects=True)
        cats = dados.listar_categorias()
        cat_id = [c for c in cats if c["nome"] == "Cat A"][0]["id"]
        admin.post("/produto", data={
            "nome": "Prod A", "quantidade_total": "5",
            "preco_locacao": "10.00", "categoria_id": str(cat_id),
            "publicado": "1",
        }, follow_redirects=True)
        _produto(admin, "Prod B", publicado=True)
        prods = dados.catalogo_produtos(categoria_id=cat_id)
        nomes = [p["nome"] for p in prods]
        assert "Prod A" in nomes
        assert "Prod B" not in nomes

    def test_kit_publico_inclui_soma_produtos(self, app, admin):
        p = _produto(admin, "Item soma", preco="40.00", publicado=True)
        k = _kit(admin, "Kit soma", preco="30.00", publicado=True)
        dados.adicionar_item_kit(k["id"], p["id"], 2)
        resultado = dados.kit_publico(k["id"])
        assert resultado["soma_produtos"] == 80.0
