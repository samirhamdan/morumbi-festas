"""Testes do modulo de clientes — Sprint 02."""

from sistema import dados


class TesteCriarCliente:
    def test_cadastrar_cliente_basico(self, app, admin):
        r = admin.post("/cliente", data={
            "nome": "Maria Silva",
            "whatsapp": "67999991234",
            "cidade": "Campo Grande",
            "origem": "Instagram",
        }, follow_redirects=True)
        assert r.status_code == 200
        clientes = dados.listar_clientes()
        assert any(c["nome"] == "Maria Silva" for c in clientes)

    def test_nome_obrigatorio(self, app, admin):
        r = admin.post("/cliente", data={
            "nome": "",
            "whatsapp": "67999991234",
        }, follow_redirects=True)
        assert "obrigatório" in r.text

    def test_todos_os_campos(self, app, admin):
        r = admin.post("/cliente", data={
            "nome": "Joao Souza",
            "cpf_cnpj": "12345678901",
            "whatsapp": "67999998888",
            "telefone": "6733334444",
            "email": "joao@teste.com",
            "data_nascimento": "1990-05-15",
            "endereco": "Rua das Flores, 123",
            "bairro": "Centro",
            "cidade": "Campo Grande",
            "cep": "79000000",
            "instagram": "joaosouza",
            "observacoes": "Cliente teste",
            "origem": "WhatsApp",
            "tags": "VIP, recorrente",
        }, follow_redirects=True)
        assert r.status_code == 200
        c = dados.listar_clientes()
        joao = [x for x in c if x["nome"] == "Joao Souza"][0]
        assert joao["cpf_cnpj"] == "12345678901"
        assert joao["email"] == "joao@teste.com"
        assert "VIP" in joao["tags"]
        assert "recorrente" in joao["tags"]


class TesteDuplicidade:
    def _criar(self, **kw):
        campos = {"nome": "Teste", "status": "ativo"}
        campos.update(kw)
        return dados.salvar_cliente(campos)

    def test_cpf_duplicado_recusado(self, app, admin):
        self._criar(nome="Ana", cpf_cnpj="11111111111")
        r = admin.post("/cliente", data={
            "nome": "Outro",
            "cpf_cnpj": "11111111111",
        }, follow_redirects=True)
        assert "já cadastrado" in r.text.lower()

    def test_whatsapp_duplicado_recusado(self, app, admin):
        self._criar(nome="Pedro", whatsapp="67999990001")
        r = admin.post("/cliente", data={
            "nome": "Outro",
            "whatsapp": "67999990001",
        }, follow_redirects=True)
        assert "já cadastrado" in r.text.lower()

    def test_email_duplicado_recusado(self, app, admin):
        self._criar(nome="Carlos", email="carlos@teste.com")
        r = admin.post("/cliente", data={
            "nome": "Outro",
            "email": "carlos@teste.com",
        }, follow_redirects=True)
        assert "já cadastrado" in r.text.lower()

    def test_mesmo_cpf_na_edicao_permite(self, app, admin):
        uid = self._criar(nome="Lucia", cpf_cnpj="22222222222")
        r = admin.post(f"/cliente/{uid}", data={
            "nome": "Lucia Editada",
            "cpf_cnpj": "22222222222",
        }, follow_redirects=True)
        assert r.status_code == 200
        c = dados.buscar_cliente(uid)
        assert c["nome"] == "Lucia Editada"

    def test_cpf_de_outro_na_edicao_recusado(self, app, admin):
        self._criar(nome="A", cpf_cnpj="33333333333")
        uid_b = self._criar(nome="B", cpf_cnpj="44444444444")
        r = admin.post(f"/cliente/{uid_b}", data={
            "nome": "B",
            "cpf_cnpj": "33333333333",
        }, follow_redirects=True)
        assert "já cadastrado" in r.text.lower()


class TesteListaClientes:
    def _popular(self):
        dados.salvar_cliente({"nome": "Ativo 1", "status": "ativo",
                              "origem": "Instagram", "cidade": "CG"},
                             tags=["VIP"])
        dados.salvar_cliente({"nome": "Ativo 2", "status": "ativo",
                              "origem": "WhatsApp", "cidade": "Dourados"})
        dados.salvar_cliente({"nome": "Inativo", "status": "inativo",
                              "cidade": "CG"})

    def test_lista_mostra_ativos(self, app, admin):
        self._popular()
        r = admin.get("/clientes")
        assert "Ativo 1" in r.text
        assert "Ativo 2" in r.text
        assert ">Inativo<" not in r.text

    def test_lista_inativos(self, app, admin):
        self._popular()
        r = admin.get("/clientes?ver=inativos")
        assert ">Inativo<" in r.text
        assert "Ativo 1" not in r.text

    def test_lista_todos(self, app, admin):
        self._popular()
        r = admin.get("/clientes?ver=todos")
        assert "Ativo 1" in r.text
        assert "Inativo" in r.text

    def test_busca_por_nome(self, app, admin):
        self._popular()
        r = admin.get("/clientes?q=Ativo+1")
        assert "Ativo 1" in r.text
        assert "Ativo 2" not in r.text

    def test_filtro_por_origem(self, app, admin):
        self._popular()
        r = admin.get("/clientes?origem=Instagram")
        assert "Ativo 1" in r.text
        assert "Ativo 2" not in r.text

    def test_filtro_por_tag(self, app, admin):
        self._popular()
        r = admin.get("/clientes?tag=VIP")
        assert "Ativo 1" in r.text
        assert "Ativo 2" not in r.text


class TesteEditarCliente:
    def test_editar_cliente(self, app, admin):
        uid = dados.salvar_cliente({"nome": "Original", "status": "ativo"})
        r = admin.post(f"/cliente/{uid}", data={
            "nome": "Editado",
            "cidade": "Dourados",
        }, follow_redirects=True)
        assert r.status_code == 200
        c = dados.buscar_cliente(uid)
        assert c["nome"] == "Editado"
        assert c["cidade"] == "Dourados"

    def test_inativar_cliente(self, app, admin):
        uid = dados.salvar_cliente({"nome": "Para inativar", "status": "ativo"})
        admin.post(f"/cliente/{uid}", data={
            "nome": "Para inativar",
            "status": "inativo",
        }, follow_redirects=True)
        c = dados.buscar_cliente(uid)
        assert c["status"] == "inativo"

    def test_cliente_inexistente_404(self, app, admin):
        r = admin.get("/cliente/9999")
        assert r.status_code == 404


class TesteTagsCliente:
    def test_criar_com_tags(self, app):
        uid = dados.salvar_cliente({"nome": "Com tags", "status": "ativo"},
                                   tags=["VIP", "recorrente"])
        c = dados.buscar_cliente(uid)
        assert "VIP" in c["tags"]
        assert "recorrente" in c["tags"]

    def test_atualizar_tags(self, app):
        uid = dados.salvar_cliente({"nome": "Tags", "status": "ativo"},
                                   tags=["VIP"])
        dados.salvar_cliente({"nome": "Tags", "status": "ativo"},
                             id_=uid, tags=["recorrente"])
        c = dados.buscar_cliente(uid)
        assert "recorrente" in c["tags"]
        assert "VIP" not in c["tags"]


class TesteWhatsApp:
    def test_link_whatsapp(self):
        assert dados.link_whatsapp("67999991234") == "https://wa.me/5567999991234"

    def test_link_whatsapp_com_55(self):
        assert dados.link_whatsapp("5567999991234") == "https://wa.me/5567999991234"

    def test_link_whatsapp_vazio(self):
        assert dados.link_whatsapp("") == ""

    def test_link_na_lista(self, app, admin):
        dados.salvar_cliente({"nome": "WPP", "whatsapp": "67999991234",
                              "status": "ativo"})
        r = admin.get("/clientes")
        assert "wa.me/5567999991234" in r.text


class TesteExportarCSV:
    def test_exportar_csv(self, app, admin):
        dados.salvar_cliente({"nome": "Export Teste", "status": "ativo",
                              "cidade": "CG", "origem": "Instagram"},
                             tags=["VIP"])
        r = admin.get("/clientes/exportar")
        assert r.status_code == 200
        assert r.content_type.startswith("text/csv")
        texto = r.data.decode("utf-8")
        assert "Export Teste" in texto
        assert "VIP" in texto

    def test_csv_nao_exporta_cpf(self, app, admin):
        dados.salvar_cliente({"nome": "Seguro", "cpf_cnpj": "99999999999",
                              "status": "ativo"})
        r = admin.get("/clientes/exportar")
        texto = r.data.decode("utf-8")
        assert "99999999999" not in texto

    def test_csv_nao_exporta_endereco(self, app, admin):
        dados.salvar_cliente({"nome": "Seguro2", "endereco": "Rua Secreta 123",
                              "status": "ativo"})
        r = admin.get("/clientes/exportar")
        texto = r.data.decode("utf-8")
        assert "Rua Secreta" not in texto


class TestePerfilClientes:
    def test_comercial_acessa_clientes(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Vendedor", "login": "vend", "perfil": "comercial"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "vend", "senha": "123"})
        r = client.get("/clientes")
        assert r.status_code == 200

    def test_operacional_nao_acessa_clientes(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Operador", "login": "oper", "perfil": "operacional"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "oper", "senha": "123"})
        r = client.get("/clientes")
        assert r.status_code == 403


class TesteAuditCliente:
    def test_criar_grava_audit(self, app, admin):
        admin.post("/cliente", data={"nome": "Audit Teste"},
                   follow_redirects=True)
        logs = dados.listar_audit()
        assert any("cliente" in (l.get("tipo") or "") for l in logs)


class TesteCartoes:
    def _checar_rotulos(self, html):
        import re
        ths = re.findall(r'<th[^>]*>(.*?)</th>', html)
        ths = [t.strip() for t in ths if t.strip()]
        rotulos = re.findall(r'data-rotulo="([^"]*)"', html)
        for rotulo in rotulos:
            assert rotulo in ths, f"data-rotulo='{rotulo}' nao encontrado nos cabecalhos {ths}"

    def test_tabela_clientes(self, app, admin):
        dados.salvar_cliente({"nome": "Rotulo Teste", "status": "ativo"})
        r = admin.get("/clientes")
        self._checar_rotulos(r.text)
