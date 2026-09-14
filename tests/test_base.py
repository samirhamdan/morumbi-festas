"""Testes de estrutura, rotas e dados basicos."""

from sistema import dados


class TesteEstruturaBasica:
    def test_seed_admin_cria_usuario(self, app):
        usuarios = dados.listar_usuarios()
        assert len(usuarios) >= 1
        admin = usuarios[0]
        assert admin["login"] == "admin"
        assert admin["perfil"] == "admin"

    def test_seed_nao_duplica(self, app):
        from sistema.auth import seed_admin
        seed_admin()
        seed_admin()
        assert len(dados.listar_usuarios()) == 1


class TesteUsuarios:
    def test_criar_usuario(self, app, admin):
        r = admin.post("/usuario", data={
            "nome": "Maria", "login": "maria",
            "senha": "abc123", "perfil": "comercial"
        }, follow_redirects=True)
        assert r.status_code == 200
        usuarios = dados.listar_usuarios()
        assert any(u["login"] == "maria" for u in usuarios)

    def test_login_duplicado_recusado(self, app, admin):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Joao", "login": "joao", "perfil": "comercial"},
            senha_hash=generate_password_hash("123"))
        r = admin.post("/usuario", data={
            "nome": "Outro Joao", "login": "joao",
            "senha": "abc", "perfil": "comercial"
        }, follow_redirects=True)
        assert "Ja existe" in r.text

    def test_nome_obrigatorio(self, app, admin):
        r = admin.post("/usuario", data={
            "nome": "", "login": "vazio",
            "senha": "abc", "perfil": "comercial"
        }, follow_redirects=True)
        assert "obrigatorio" in r.text

    def test_login_obrigatorio(self, app, admin):
        r = admin.post("/usuario", data={
            "nome": "Teste", "login": "",
            "senha": "abc", "perfil": "comercial"
        }, follow_redirects=True)
        assert "obrigatorio" in r.text

    def test_editar_usuario(self, app, admin):
        from werkzeug.security import generate_password_hash
        uid = dados.salvar_usuario(
            {"nome": "Ana", "login": "ana", "perfil": "comercial"},
            senha_hash=generate_password_hash("123"))
        r = admin.post(f"/usuario/{uid}", data={
            "nome": "Ana Maria", "login": "ana",
            "perfil": "gestor"
        }, follow_redirects=True)
        assert r.status_code == 200
        u = dados.buscar_usuario(uid)
        assert u["nome"] == "Ana Maria"
        assert u["perfil"] == "gestor"

    def test_inativar_usuario(self, app, admin):
        from werkzeug.security import generate_password_hash
        uid = dados.salvar_usuario(
            {"nome": "Pedro", "login": "pedro", "perfil": "comercial"},
            senha_hash=generate_password_hash("123"))
        admin.post(f"/usuario/{uid}", data={
            "nome": "Pedro", "login": "pedro",
            "perfil": "comercial", "ativo": "0"
        }, follow_redirects=True)
        u = dados.buscar_usuario(uid)
        assert u["ativo"] == 0

    def test_usuario_inativo_nao_faz_login(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Inativo", "login": "inativo", "perfil": "comercial",
             "ativo": 0},
            senha_hash=generate_password_hash("123"))
        r = client.post("/entrar", data={"login": "inativo", "senha": "123"},
                        follow_redirects=True)
        assert "incorretos" in r.text

    def test_lista_usuarios_filtra_ativos(self, app, admin):
        r = admin.get("/usuarios")
        assert r.status_code == 200
        assert "Administrador" in r.text

    def test_lista_usuarios_busca(self, app, admin):
        r = admin.get("/usuarios?q=admin")
        assert "Administrador" in r.text

    def test_editar_usuario_inexistente_404(self, app, admin):
        r = admin.get("/usuario/9999")
        assert r.status_code == 404


class TesteDashboard:
    def test_dashboard_carrega(self, logado):
        r = logado.get("/")
        assert r.status_code == 200
        assert "Bem-vindo" in r.text

    def test_dashboard_mostra_aviso_sem_senha(self, app):
        import os
        senha_original = os.environ.get("FESTAS_SENHA")
        os.environ["FESTAS_SENHA"] = ""
        c = app.test_client()
        with c.session_transaction() as s:
            s["usuario_id"] = 1
            s["perfil"] = "admin"
            s["usuario_nome"] = "Admin"
        r = c.get("/")
        assert "FESTAS_SENHA" in r.text
        if senha_original:
            os.environ["FESTAS_SENHA"] = senha_original


class TesteParametros:
    def test_salvar_e_ler_parametro(self, app):
        dados.salvar_parametro("teste_chave", "teste_valor")
        assert dados.parametro("teste_chave") == "teste_valor"

    def test_parametro_padrao(self, app):
        assert dados.parametro("inexistente", "padrao") == "padrao"


class TesteOrganizacao:
    def test_salvar_e_ler_organizacao(self, app):
        dados.salvar_organizacao({"nome": "Morumbi Festas", "cidade": "Campo Grande"})
        org = dados.organizacao()
        assert org["nome"] == "Morumbi Festas"
        assert org["cidade"] == "Campo Grande"

    def test_atualizar_organizacao(self, app):
        dados.salvar_organizacao({"nome": "Morumbi Festas"})
        dados.salvar_organizacao({"nome": "Morumbi Festas LTDA"})
        org = dados.organizacao()
        assert org["nome"] == "Morumbi Festas LTDA"


class TesteRotasMenu:
    def test_todas_as_rotas_do_menu_carregam(self, admin):
        from sistema.app import MENU
        for grupo, itens in MENU:
            for ep, rotulo in itens:
                from flask import url_for
                with admin.application.test_request_context():
                    url = url_for(ep)
                r = admin.get(url)
                assert r.status_code == 200, f"Rota {ep} ({url}) retornou {r.status_code}"


class TesteCartoes:
    """Toda celula td tem data-rotulo igual ao th da mesma coluna."""

    def _checar_rotulos(self, html):
        import re
        ths = re.findall(r'<th[^>]*>(.*?)</th>', html)
        ths = [t.strip() for t in ths if t.strip()]
        rotulos = re.findall(r'data-rotulo="([^"]*)"', html)
        for rotulo in rotulos:
            assert rotulo in ths, f"data-rotulo='{rotulo}' nao encontrado nos cabeçalhos {ths}"

    def test_tabela_usuarios(self, admin):
        r = admin.get("/usuarios")
        self._checar_rotulos(r.text)
