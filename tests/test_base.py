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
        assert "Já existe" in r.text

    def test_nome_obrigatorio(self, app, admin):
        r = admin.post("/usuario", data={
            "nome": "", "login": "vazio",
            "senha": "abc", "perfil": "comercial"
        }, follow_redirects=True)
        assert "obrigatório" in r.text

    def test_login_obrigatorio(self, app, admin):
        r = admin.post("/usuario", data={
            "nome": "Teste", "login": "",
            "senha": "abc", "perfil": "comercial"
        }, follow_redirects=True)
        assert "obrigatório" in r.text

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
        assert "desativado" in r.text  # senha certa, vínculo inativo

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
        assert "Olá, Administrador!" in r.text

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
            for ep, rotulo, _icone, _extras in itens:
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


class TesteMenuLateral:
    def test_menu_agrupado_com_icones_e_item_ativo(self, admin):
        r = admin.get("/pedidos")
        for grupo in ("Vendas", "Pedidos", "Catálogo", "Gestão"):
            assert f'<p class="lateral-titulo">{grupo}</p>' in r.text
        assert 'href="#mi-pedido"' in r.text or '#mi-pedido' in r.text
        assert r.text.count('aria-current="page"') >= 1
        assert 'id="lateral-recolher"' in r.text

    def test_tela_interna_acende_o_item(self, admin):
        from sistema.app import _menu_do_usuario
        menu = _menu_do_usuario(lambda ep: True, "editar_kit")
        ativos = [l["rotulo"] for _, links in menu for l in links if l["ativo"]]
        assert ativos == ["Produtos e kits"]
        menu = _menu_do_usuario(lambda ep: True, "lista_origens")
        assert [l["rotulo"] for _, links in menu for l in links if l["ativo"]] == ["Configurações"]

    def test_configuracoes_leva_a_primeira_tela_permitida(self, admin):
        from sistema.app import _menu_do_usuario
        menu = _menu_do_usuario(lambda ep: ep in ("lista_usuarios", "painel"), "painel")
        itens = {l["rotulo"]: l["ep"] for _, links in menu for l in links}
        assert itens == {"Início": "painel", "Configurações": "lista_usuarios"}

    def test_perfil_operacional_ve_so_o_que_pode(self, app):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario({"nome": "Op", "login": "op_menu", "perfil": "operacional"},
                             senha_hash=generate_password_hash("123"))
        c = app.test_client()
        c.post("/entrar", data={"login": "op_menu", "senha": "123"})
        r = c.get("/agenda")
        assert 'data-dica="Agenda"' in r.text
        assert 'data-dica="Configurações"' not in r.text
        assert 'data-dica="Relatórios"' not in r.text

    def test_celular_tem_barra_de_atalhos_com_menu(self, admin):
        r = admin.get("/agenda")
        assert 'class="barra-baixo"' in r.text and 'id="barra-baixo-menu"' in r.text
        # até 4 atalhos + Menu; a tela atual fica marcada
        assert r.text.count('class="barra-baixo-item') == 5
        assert 'class="barra-baixo-item aqui" aria-current="page"' in r.text

    def test_atalhos_seguem_as_permissoes(self):
        from sistema.app import _atalhos_celular, _menu_do_usuario
        pode = {"painel", "agenda", "painel_operacional", "lista_produtos"}
        menu = _menu_do_usuario(lambda ep: ep in pode, "agenda")
        rotulos = [a["rotulo"] for a in _atalhos_celular(menu)]
        assert rotulos == ["Início", "Agenda", "Esteira", "Produtos"]

    def test_sem_login_nao_tem_barra(self, app):
        r = app.test_client().get("/entrar")
        assert 'class="barra-baixo"' not in r.text
