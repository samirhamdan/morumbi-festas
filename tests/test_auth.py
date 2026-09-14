"""Testes de autenticacao e perfis."""

from sistema import dados


class TesteLogin:
    def test_tela_login_carrega(self, client):
        r = client.get("/entrar")
        assert r.status_code == 200
        assert "Morumbi" in r.text
        assert "Festas" in r.text

    def test_login_valido_redireciona(self, client):
        r = client.post("/entrar", data={"login": "admin", "senha": "teste123"},
                        follow_redirects=False)
        assert r.status_code == 302

    def test_login_invalido_mostra_erro(self, client):
        r = client.post("/entrar", data={"login": "admin", "senha": "errada"},
                        follow_redirects=True)
        assert "incorretos" in r.text

    def test_login_inexistente(self, client):
        r = client.post("/entrar", data={"login": "naoexiste", "senha": "x"},
                        follow_redirects=True)
        assert "incorretos" in r.text

    def test_logout_redireciona_para_login(self, logado):
        r = logado.get("/sair", follow_redirects=False)
        assert r.status_code == 302
        assert "/entrar" in r.headers["Location"]

    def test_rota_protegida_redireciona_sem_login(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/entrar" in r.headers["Location"]


class TestePerfis:
    def test_admin_acessa_usuarios(self, admin):
        r = admin.get("/usuarios")
        assert r.status_code == 200

    def test_comercial_nao_acessa_usuarios(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Vendedor", "login": "vend", "perfil": "comercial"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "vend", "senha": "123"})
        r = client.get("/usuarios")
        assert r.status_code == 403

    def test_operacional_nao_acessa_usuarios(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Operador", "login": "oper", "perfil": "operacional"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "oper", "senha": "123"})
        r = client.get("/usuarios")
        assert r.status_code == 403

    def test_gestor_nao_acessa_usuarios(self, app, client):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario(
            {"nome": "Gestor", "login": "gest", "perfil": "gestor"},
            senha_hash=generate_password_hash("123"))
        client.post("/entrar", data={"login": "gest", "senha": "123"})
        r = client.get("/usuarios")
        assert r.status_code == 403


class TesteAuditoria:
    def test_login_grava_audit(self, app, client):
        client.post("/entrar", data={"login": "admin", "senha": "teste123"})
        logs = dados.listar_audit()
        assert any("entrou" in (l.get("descricao") or "") for l in logs)

    def test_logout_grava_audit(self, logado):
        logado.get("/sair")
        logs = dados.listar_audit()
        assert any("Saiu" in (l.get("descricao") or "") for l in logs)
