"""Login, recuperação de senha e Personalização do Login."""

import io
import os
import re

from PIL import Image
from werkzeug.security import generate_password_hash

from sistema import correio, dados

SENHA = "segredo123"


def _usuario(login="maria", email="", perfil="comercial", senha=SENHA, ativo=1):
    return dados.salvar_usuario({"nome": login.title(), "login": login, "email": email,
                                 "perfil": perfil, "ativo": ativo},
                                senha_hash=generate_password_hash(senha))


def _entrar(client, login, senha=SENHA, **extra):
    return client.post("/entrar", data={"login": login, "senha": senha, **extra})


def _png(cor=(111, 28, 133, 255), tamanho=(60, 40)):
    b = io.BytesIO()
    Image.new("RGBA", tamanho, cor).save(b, "PNG")
    return b.getvalue()


def _jpg(tamanho=(3000, 2000)):
    b = io.BytesIO()
    Image.new("RGB", tamanho, (200, 120, 40)).save(b, "JPEG")
    return b.getvalue()


def _token(html):
    return re.search(r"/redefinir-senha/([A-Za-z0-9_-]+)", html).group(1)


def _auditoria(acao):
    with dados.conectar() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE dados LIKE ? ORDER BY id", (f'%"acao": "{acao}"%',))]


# --- Autenticação ------------------------------------------------------------

class TesteAutenticacao:
    def test_tela_nova_sem_login_social(self, client):
        r = client.get("/entrar")
        t = r.text
        for texto in ("Bem-vindo(a)", "Acesse sua conta e continue gerenciando as festas",
                      "SEU MOMENTO, SUA FESTA", "MORUMBI FESTAS", "Usuário ou e-mail", "Senha",
                      "Lembrar de mim", "Esqueceu a senha?", "Entrar", "Acesso seguro e protegido",
                      "logo-morumbi.webp"):
            assert texto in t, texto
        for social in ("entrar com google", "login social", "facebook login", "apple"):
            assert social not in t.lower(), social
        assert 'for="lg-login"' in t and 'autocomplete="username"' in t
        assert 'autocomplete="current-password"' in t and 'type="password"' in t
        assert 'aria-describedby="lg-senha-erro"' in t and 'aria-controls="lg-senha"' in t
        assert r.headers["Cache-Control"] == "no-store"
        assert r.headers["X-Frame-Options"] == "SAMEORIGIN"

    def test_usuario_e_senha_corretos(self, app, client):
        _usuario()
        r = _entrar(client, "maria")
        assert r.status_code == 302 and r.headers["Location"].endswith("/")
        assert client.get("/").status_code == 200
        assert client.get("/entrar").status_code == 302  # já logado vai ao Dashboard

    def test_entra_pelo_email_unico(self, app, client):
        _usuario(email="maria@festas.com")
        assert _entrar(client, "Maria@Festas.com").status_code == 302

    def test_email_repetido_nao_identifica(self, app, client):
        _usuario("ana", email="mesmo@x.com")
        _usuario("bia", email="mesmo@x.com")
        assert "Usuário ou senha inválidos." in _entrar(client, "mesmo@x.com").text

    def test_erro_nao_revela_se_usuario_existe(self, app, client):
        _usuario()
        errada = _entrar(client, "maria", "outra")
        inexistente = _entrar(client, "fulano", "outra")
        for r in (errada, inexistente):
            assert r.status_code == 200 and "Usuário ou senha inválidos." in r.text
        assert 'value="maria"' in errada.text  # mantém o que foi digitado
        assert 'value="outra"' not in errada.text  # senha nunca volta na página
        assert "session" not in (errada.headers.get("Set-Cookie") or "") or \
            not client.get("/", follow_redirects=False).status_code == 200

    def test_campos_vazios(self, client):
        r = client.post("/entrar", data={"login": "", "senha": ""})
        assert "Informe seu usuário ou e-mail." in r.text and "Informe sua senha." in r.text
        assert 'aria-invalid="true"' in r.text

    def test_lembrar_de_mim(self, app, client):
        _usuario()
        com = _entrar(client, "maria", lembrar="1").headers["Set-Cookie"]
        assert "Expires=" in com
        outro = app.test_client()
        sem = _entrar(outro, "maria").headers["Set-Cookie"]
        assert "Expires=" not in sem and "HttpOnly" in sem and "SameSite=Lax" in sem

    def test_muitas_tentativas_bloqueiam_e_acerto_limpa(self, app, client):
        _usuario()
        for _ in range(dados.LIMITE_FALHAS_LOGIN):
            _entrar(client, "maria", "errada")
        r = _entrar(client, "maria")  # até a senha certa espera
        assert "Muitas tentativas" in r.text and r.status_code == 200
        with dados.conectar() as conn:
            conn.execute("UPDATE tentativas_login SET criado_em = '2000-01-01T00:00:00'")
        assert _entrar(client, "maria").status_code == 302
        with dados.conectar() as conn:
            chave = dados._chaves_tentativa("maria", "")[0]
            assert conn.execute("SELECT COUNT(*) FROM tentativas_login WHERE chave = ?",
                                (chave,)).fetchone()[0] == 0

    def test_usuario_desativado(self, app, client):
        _usuario(ativo=0)
        r = _entrar(client, "maria")
        assert r.status_code == 200 and "desativado" in r.text

    def test_sessao_e_logout(self, app, client):
        _usuario()
        _entrar(client, "maria")
        r = client.get("/sair")
        assert r.status_code == 302 and r.headers["Location"].endswith("/entrar")
        assert client.get("/pedidos").status_code == 302


# --- Recuperação de senha ------------------------------------------------------

class TesteRecuperacaoDeSenha:
    def test_pedido_tem_resposta_unica_e_aparece_para_o_admin(self, app, client, admin):
        uid = _usuario()
        for ident in ("maria", "ninguem"):
            r = client.post("/esqueci-senha", data={"login": ident})
            assert "Se houver uma conta ativa com esses dados" in r.text
        with dados.conectar() as conn:
            linhas = conn.execute("SELECT usuario_id, origem, token_hash FROM redefinicoes_senha"
                                  ).fetchall()
        assert [(r["usuario_id"], r["origem"]) for r in linhas] == [(uid, "pedido")]
        assert len(linhas[0]["token_hash"]) == 64  # só o hash fica no banco
        client.post("/esqueci-senha", data={"login": "maria"})  # repetido: não duplica
        with dados.conectar() as conn:
            assert conn.execute("SELECT COUNT(*) FROM redefinicoes_senha").fetchone()[0] == 1
        assert _auditoria("pedir_senha")
        u = admin.get("/usuarios").text
        assert "Pedidos de nova senha" in u and "Maria" in u
        assert client.post("/esqueci-senha", data={"login": ""}).text.count(
            "Informe seu usuário ou e-mail.") >= 1

    def test_link_do_admin_redefine_uma_vez(self, app, client, admin):
        uid = _usuario()
        r = admin.post(f"/usuario/{uid}/link-senha")
        assert "Link de nova senha para Maria" in r.text and "Enviar por WhatsApp" in r.text
        token = _token(r.text)
        assert client.get(f"/redefinir-senha/{token}").status_code == 200
        curta = client.post(f"/redefinir-senha/{token}", data={"senha": "123", "confirmacao": "123"})
        assert "pelo menos 8 caracteres" in curta.text
        diferente = client.post(f"/redefinir-senha/{token}",
                                data={"senha": "novasenha1", "confirmacao": "novasenha2"})
        assert "As senhas não conferem." in diferente.text
        ok = client.post(f"/redefinir-senha/{token}",
                         data={"senha": "novasenha1", "confirmacao": "novasenha1"},
                         follow_redirects=True)
        assert "Senha alterada. Entre com a nova senha." in ok.text
        outro = app.test_client()
        assert "inválidos" in _entrar(outro, "maria").text
        assert _entrar(outro, "maria", "novasenha1").status_code == 302
        de_novo = client.get(f"/redefinir-senha/{token}")
        assert de_novo.status_code == 410 and "Link expirado" in de_novo.text
        assert _auditoria("gerar_link_senha") and _auditoria("redefinir_senha")

    def test_link_expira(self, app, client, admin):
        uid = _usuario()
        token = _token(admin.post(f"/usuario/{uid}/link-senha").text)
        with dados.conectar() as conn:
            conn.execute("UPDATE redefinicoes_senha SET expira_em = '2000-01-01T00:00:00'")
        assert client.get(f"/redefinir-senha/{token}").status_code == 410
        assert client.get("/redefinir-senha/qualquer-coisa").status_code == 410

    def test_email_quando_configurado(self, app, client, monkeypatch):
        monkeypatch.setenv("FESTAS_SMTP_HOST", "smtp.exemplo.com")
        monkeypatch.setenv("FESTAS_SMTP_REMETENTE", "sistema@exemplo.com")
        assert not correio.configurado()  # sem o endereço público, não envia
        monkeypatch.setenv("FESTAS_URL_BASE", "https://morumbifestas.duckdns.org")
        assert correio.configurado()
        enviados = []
        monkeypatch.setattr(correio, "enviar", lambda para, assunto, texto: enviados.append((para, texto)))
        _usuario(email="maria@festas.com")
        client.post("/esqueci-senha", data={"login": "maria@festas.com"},
                    headers={"Host": "site-falso.com"})
        assert enviados and enviados[0][0] == "maria@festas.com"
        assert "https://morumbifestas.duckdns.org/redefinir-senha/" in enviados[0][1]
        assert "site-falso" not in enviados[0][1]
        assert dados.link_de_senha_valido(_token(enviados[0][1]))

    def test_admin_de_outra_empresa_nao_gera_link(self, app, client):
        uid = _usuario()
        b = dados.criar_empresa("Empresa B")
        with dados.usando_tenant(b):
            dados.salvar_usuario({"nome": "Chefe B", "login": "chefeb", "perfil": "admin"},
                                 senha_hash=generate_password_hash(SENHA))
        _entrar(client, "chefeb")
        r = client.post(f"/usuario/{uid}/link-senha", follow_redirects=True)
        assert "/redefinir-senha/" not in r.text
        with dados.conectar() as conn:
            assert conn.execute("SELECT COUNT(*) FROM redefinicoes_senha").fetchone()[0] == 0


# --- Personalização do Login ---------------------------------------------------

def _salvar(admin, **campos):
    base = {k: dados.config_login()[k] for k in
            (*dados.CORES_LOGIN, "login_layout", "login_modelo", *dados.TEXTOS_LOGIN)}
    base.update(campos)
    return admin.post("/configuracoes/login", data=base, content_type="multipart/form-data",
                      headers={"X-Requested-With": "fetch"})


class TestePersonalizacao:
    def test_permissoes_no_servidor(self, app, client):
        _usuario(perfil="comercial")
        _entrar(client, "maria")
        for metodo, url in (("get", "/configuracoes/login"), ("post", "/configuracoes/login"),
                            ("post", "/configuracoes/login/restaurar"),
                            ("get", "/configuracoes/login/previa")):
            assert getattr(client, metodo)(url).status_code == 403, url
        assert dados.config("login_titulo") == "Bem-vindo(a)"

    def test_tela_modelos_e_previa(self, admin):
        t = admin.get("/configuracoes/login").text
        for texto in ("Personalização do Login", "Modelos prontos", "Padrão", "Minimalista",
                      "Escuro", "Foto real", "Clean", "Dividido", "Centralizado", "Trocar logo",
                      "Imagem de fundo", "Cor primária", "Cor do botão", "Fundo do login",
                      "Texto principal", "Texto secundário", "Salvar alterações", "Restaurar",
                      "Restaurar o login para o padrão?", "Restaurar padrão", "Desktop", "Tablet",
                      "Celular", "/configuracoes/login/previa"):
            assert texto in t, texto
        previa = admin.get("/configuracoes/login/previa").text
        assert "data-previa" in previa and "lg-previa" in previa

    def test_salvar_cores_textos_layout_e_auditoria(self, app, admin):
        client = app.test_client()  # visitante sem login
        r = _salvar(admin, cor_primaria="#7c3aed", login_cor_botao="#FF8C00",
                    login_layout="centralizado", login_modelo="clean",
                    login_titulo="Olá, <script>alert(1)</script>equipe",
                    login_subtitulo="Entre para ver a agenda.")
        assert r.status_code == 200 and r.get_json()["ok"]
        c = dados.config_login()
        assert (c["cor_primaria"], c["login_layout"], c["login_modelo"]) == ("#7C3AED", "centralizado", "clean")
        assert c["login_titulo"] == "Olá, alert(1)equipe"  # marcação removida
        assert c["texto_botao"] == "#1E1C22"  # botão claro: texto escuro
        t = client.get("/entrar").text
        assert "lg--centralizado" in t and "--lg-primaria: #7C3AED" in t
        assert "Entre para ver a agenda." in t and "<script>alert" not in t
        registro = _auditoria("alterar")[-1]
        assert "cor_primaria" in registro["dados"] and "#6F1C85" in registro["dados"]
        assert registro["usuario_id"] == 1
        # cores são as mesmas da identidade visual (Configurações → Sistema)
        assert dados.config("cor_primaria") == "#7C3AED"

    def test_validacoes(self, admin):
        r = _salvar(admin, login_cor_botao="vermelho")
        assert r.status_code == 400 and r.get_json()["campo"] == "login_cor_botao"
        assert _salvar(admin, login_titulo="  <b></b> ").get_json()["campo"] == "login_titulo"
        assert _salvar(admin, login_layout="lateral").status_code == 400
        assert _salvar(admin, login_modelo="neon").status_code == 400
        assert dados.config_login()["login_cor_botao"] == "#6F1C85"

    def test_logo_png_svg_remover_e_fallback(self, app, admin):
        client = app.test_client()
        pasta = os.path.join(app.static_folder, "uploads", "empresas")
        criados = []
        try:
            r = admin.post("/configuracoes/login", data={
                **{k: dados.config_login()[k] for k in (*dados.CORES_LOGIN, "login_layout",
                                                          "login_modelo", *dados.TEXTOS_LOGIN)},
                "logo": (io.BytesIO(_png()), "marca.png")},
                content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
            assert r.get_json()["ok"]
            logo = dados.empresa_atual()["logo"]
            criados.append(logo)
            assert logo.endswith(".png") and os.path.exists(os.path.join(app.static_folder, logo))
            assert logo in client.get("/entrar").text
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="4" cx="5" cy="5"/></svg>'
            _salvar(admin, logo=(io.BytesIO(svg), "logo.svg"))
            criados.append(dados.empresa_atual()["logo"])
            assert dados.empresa_atual()["logo"].endswith(".svg")
            for ruim, nome in ((b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "x.svg"),
                               (b"\x89PNG\r\n\x1a\nfalso", "x.png"), (b"MZ\x90\x00", "x.jpg")):
                r = _salvar(admin, logo=(io.BytesIO(ruim), nome))
                assert r.status_code == 400 and r.get_json()["campo"] == "logo", nome
            grande = _salvar(admin, logo=(io.BytesIO(b"0" * (2 * 1024 * 1024 + 10)), "g.png"))
            assert "2 MB" in grande.get_json()["mensagem"]
            _salvar(admin, remover_logo="1")
            assert dados.empresa_atual()["logo"] == ""
            assert "logo-morumbi.webp" in client.get("/entrar").text
        finally:
            for c in criados:
                arquivo = os.path.join(app.static_folder, c)
                if os.path.exists(arquivo):
                    os.remove(arquivo)

    def test_imagem_de_fundo_comprimida_e_removida(self, app, admin):
        client = app.test_client()
        r = _salvar(admin, imagem=(io.BytesIO(_jpg()), "festa.jpg"))
        assert r.get_json()["ok"]
        caminho = dados.config("login_imagem")
        arquivo = os.path.join(app.static_folder, caminho)
        try:
            assert caminho.endswith(".webp")
            assert max(Image.open(arquivo).size) == 1920
            assert "lg--com-imagem" in client.get("/entrar").text
            _salvar(admin, remover_imagem="1")
            assert dados.config("login_imagem") == ""
            assert "lg--com-imagem" not in client.get("/entrar").text
        finally:
            os.remove(arquivo)

    def test_restaurar_padrao(self, app, admin, client):
        _salvar(admin, cor_primaria="#123456", login_titulo="Outro título", login_layout="centralizado")
        dados.salvar_logo_empresa("uploads/empresas/1/logo-x.png")
        r = admin.post("/configuracoes/login/restaurar", follow_redirects=True)
        assert "Login restaurado para o padrão." in r.text
        c = dados.config_login()
        assert (c["cor_primaria"], c["login_titulo"], c["login_layout"], c["logo"]) == (
            "#6F1C85", "Bem-vindo(a)", "dividido", "")
        mudancas = _auditoria("restaurar")[-1]["dados"]
        assert "Outro título" in mudancas and "logo-x.png" in mudancas

    def test_fallback_com_configuracao_invalida(self, app, client):
        dados.salvar_config({"cor_primaria": "red", "login_layout": "xyz", "login_modelo": "?",
                             "login_imagem": "uploads/empresas/1/a');x:y.png",
                             "login_titulo": "<b></b>"})
        dados.salvar_logo_empresa("http://externo/logo.png")
        c = dados.config_login()
        assert (c["cor_primaria"], c["login_layout"], c["login_modelo"], c["login_imagem"],
                c["login_titulo"], c["logo"]) == ("#6F1C85", "dividido", "padrao", "",
                                                   "Bem-vindo(a)", "")
        r = client.get("/entrar")
        assert r.status_code == 200 and "logo-morumbi.webp" in r.text

    def test_api_publica_uma_chamada(self, client):
        r = client.get("/api/login/config")
        j = r.get_json()
        assert r.status_code == 200 and j["login_titulo"] == "Bem-vindo(a)"
        assert j["logo_url"].endswith("logo-morumbi.webp") and j["imagem_url"] == ""
        assert set(dados.CHAVES_LOGIN) <= set(j)
        assert "senha" not in r.text.lower()

    def test_cache_de_configuracao_expira(self, app, monkeypatch):
        dados.config("login_titulo")
        with dados.conectar() as conn:  # outro processo gravou direto no banco
            conn.execute("INSERT INTO configuracoes (tenant_id, chave, valor, atualizado_em)"
                         " VALUES (?, 'login_titulo', 'Novo', '2026-01-01')", (dados.tenant_padrao(),))
        assert dados.config("login_titulo") == "Bem-vindo(a)"
        agora = dados.time.monotonic()
        monkeypatch.setattr(dados.time, "monotonic", lambda: agora + dados.CONFIG_CACHE_SEGUNDOS + 1)
        assert dados.config("login_titulo") == "Novo"

    def test_menu_configuracoes(self, admin):
        t = admin.get("/configuracoes/empresa").text
        assert 'href="/configuracoes/login"' in t and "Personalização do Login" in t
