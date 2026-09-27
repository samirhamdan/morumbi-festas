"""Aplicacao Flask — Morumbi Festas."""

import io
import os
import re
import threading
import time
from urllib.parse import quote
from datetime import timedelta

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from sistema import auth, correio, dados, formato, imagens, listas, permissoes

UPLOAD_EXTENSOES = {".jpg", ".jpeg", ".png", ".webp"}
UPLOAD_MAX_MB = 10


# Menu lateral, organizado pelo caminho do trabalho: vender -> atender o
# pedido -> manter o catálogo -> acompanhar e configurar.
# Cada item: (endpoint, rótulo, ícone, telas internas que também acendem o item).
# O primeiro endpoint que o usuário pode abrir é o destino do link.
CONFIG_TELAS = ("config_empresa", "lista_usuarios", "config_permissoes", "config_operacao",
                "config_sistema", "config_login", "lista_origens")
MENU = (
    ("", (
        ("painel", "Início", "inicio", ()),
    )),
    ("Vendas", (
        ("lista_leads", "Leads", "funil", ("editar_lead",)),
        ("lista_orcamentos", "Orçamentos", "orcamento", ("editar_orcamento",)),
        ("lista_clientes", "Clientes", "clientes", ("editar_cliente",)),
    )),
    ("Pedidos", (
        ("lista_pedidos", "Pedidos", "pedido", ("ver_pedido", "editar_pedido",
                                                "ver_pedido_historico")),
        ("agenda", "Agenda", "agenda", ()),
        ("painel_operacional", "Esteira", "esteira", ("ver_pedido_operacional",)),
    )),
    ("Catálogo", (
        ("lista_produtos", "Produtos e kits", "produto", ("lista_kits", "editar_produto",
                                                          "editar_kit", "disponibilidade_produto")),
        ("lista_categorias", "Categorias", "categoria", ()),
        ("catalogo_interno", "Vitrine", "vitrine", ()),
    )),
    ("Gestão", (
        ("faturamento", "Relatórios", "relatorio", ()),
        ("config_empresa", "Configurações", "config",
         CONFIG_TELAS[1:] + ("configuracoes", "editar_usuario", "config_servicos")),
    )),
)

# Abas da área de Configurações (endpoint, rótulo).
ABAS_CONFIG = (
    ("config_empresa", "Empresa"), ("lista_usuarios", "Usuários"),
    ("config_permissoes", "Permissões"), ("config_operacao", "Operação"),
    ("config_sistema", "Sistema"), ("config_login", "Personalização do Login"),
    ("lista_origens", "Origens de lead"),
)
LOGO_MAX_BYTES = 2 * 1024 * 1024


def _grupo_de(endpoint: str) -> str:
    for grupo, itens in MENU:
        for ep, _, _, extras in itens:
            if endpoint == ep or endpoint in extras:
                return grupo
    return ""


def _menu_do_usuario(pode, endpoint: str) -> list:
    """Menu já filtrado pelas permissões: [(grupo, [{href, rotulo, icone, ativo}])]."""
    saida = []
    for grupo, itens in MENU:
        links = []
        for ep, rotulo, icone, extras in itens:
            destinos = [ep] + ([e for e in CONFIG_TELAS if e != ep] if ep == "config_empresa" else [])
            destino = next((d for d in destinos if pode(d)), None)
            if destino:
                links.append({"ep": destino, "rotulo": rotulo, "icone": icone,
                              "ativo": endpoint == ep or endpoint in extras})
        if links:
            saida.append((grupo, links))
    return saida


def _erro(exc):
    campo = getattr(exc, "campo", None)
    return {"erro": str(exc), "campo_erro": campo}


def criar_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder="static")
    app.secret_key = os.environ.get("FESTAS_SECRET", "dev-morumbi-festas-2026")
    # Atrás do proxy (Caddy): endereço real do visitante e https nos links.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))

    formato.registrar(app)
    dados.inicializar()
    auth.seed_admin()

    _css_path = os.path.join(app.static_folder, "sistema.css")
    _css_ver = int(os.path.getmtime(_css_path)) if os.path.exists(_css_path) else 0

    def _pode(endpoint: str) -> bool:
        return auth.pode_acessar(endpoint, app.view_functions)

    # Empresa atual de cada requisição: a do vínculo do usuário autenticado;
    # sem login (entrar, catálogo público), a empresa principal.
    @app.before_request
    def _empresa_da_requisicao():
        m = auth.membro_atual()
        g._token_empresa = dados.definir_tenant(
            m["tenant_id"] if m else dados.tenant_padrao())

    @app.teardown_request
    def _fim_da_requisicao(_erro=None):
        token = g.pop("_token_empresa", None)
        if token is not None:
            dados.restaurar_tenant(token)

    @app.context_processor
    def contexto_global():
        m = auth.membro_atual()
        return {
            "perfil": m["perfil"] if m else "",
            "rotulo_perfil": dados.ROTULOS_PERFIL.get(m["perfil"], "") if m else "",
            "tem": auth.tem,
            "empresa": dados.empresa_atual(),
            "usuario_nome": session.get("usuario_nome", ""),
            "usuario_id": session.get("usuario_id"),
            "com_senha": auth.com_senha(),
            "menu": MENU,
            "menu_usuario": _menu_do_usuario(_pode, request.endpoint or "") if m else [],
            "abas_config": ABAS_CONFIG,
            "grupo_ativo": _grupo_de(request.endpoint or ""),
            "css_ver": _css_ver,
            "pode": _pode,
            "rotulo_fonte": dados.rotulo_fonte,
            "tipos_ocorrencia": dados.TIPOS_OCORRENCIA,
        }

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _render_login(template="login.html", status=200, **ctx):
        resposta = app.make_response((render_template(
            template, lg=dados.config_login(), **ctx), status))
        # a página de login nunca é guardada por navegador ou proxy nem
        # exibida dentro de outro site (só na prévia do próprio sistema)
        resposta.headers["Cache-Control"] = "no-store"
        resposta.headers["X-Frame-Options"] = "SAMEORIGIN"
        resposta.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        return resposta

    def _link_absoluto(endpoint, **valores):
        base = os.environ.get("FESTAS_URL_BASE", "").rstrip("/")
        return (base + url_for(endpoint, **valores)) if base else url_for(
            endpoint, _external=True, **valores)

    @app.route("/entrar", methods=["GET", "POST"])
    def entrar():
        if request.method == "GET":
            if session.get("usuario_id") and auth.membro_atual():
                return redirect(url_for("painel"))
            return _render_login()
        login_ = (request.form.get("login") or "").strip().lower()
        senha = request.form.get("senha") or ""
        lembrar = request.form.get("lembrar") == "1"
        erros = {}
        if not login_:
            erros["login"] = "Informe seu usuário ou e-mail."
        if not senha:
            erros["senha"] = "Informe sua senha."
        if erros:
            return _render_login(erros=erros, login_digitado=login_, lembrar=lembrar)
        ip = request.remote_addr or ""
        if dados.login_bloqueado(login_, ip):
            return _render_login(erro_geral="Muitas tentativas seguidas. Aguarde alguns"
                                 " minutos e tente de novo.", login_digitado=login_,
                                 lembrar=lembrar)
        try:
            u = auth.login(login_, senha)
        except auth.AcessoNegado as e:
            return _render_login(erro_geral=str(e), login_digitado=login_, lembrar=lembrar)
        if not u:
            dados.registrar_falha_login(login_, ip)
            # mesma resposta para usuário inexistente ou senha errada
            return _render_login(erro_geral="Usuário ou senha inválidos.",
                                 login_digitado=login_, lembrar=lembrar)
        dados.limpar_falhas_login(login_)
        session.permanent = lembrar
        dados.registrar_acao(u["id"], "login", f"{u['nome']} entrou")
        return redirect(url_for("painel"))

    MENSAGEM_PEDIDO_SENHA = (
        "Se houver uma conta ativa com esses dados, o pedido foi registrado."
        " Você receberá o link por e-mail, quando houver e-mail cadastrado, ou pelo"
        " administrador do sistema. O link vale por 1 hora.")

    @app.route("/esqueci-senha", methods=["GET", "POST"])
    def esqueci_senha():
        if request.method == "GET":
            return _render_login("senha_esqueci.html")
        ident = (request.form.get("login") or "").strip().lower()
        if not ident:
            return _render_login("senha_esqueci.html", status=400, login_digitado=ident,
                                 erros={"login": "Informe seu usuário ou e-mail."})
        resultado = dados.pedir_nova_senha(ident)
        if resultado:
            u, token = resultado
            if u.get("email") and correio.configurado():
                correio.enviar(
                    u["email"], "Nova senha — " + dados.empresa_atual()["nome_exibicao"],
                    f"Olá, {u['nome']}.\n\nPara criar uma nova senha, abra o link abaixo"
                    f" (válido por 1 hora):\n\n{_link_absoluto('redefinir_senha', token=token)}"
                    "\n\nSe você não pediu, ignore esta mensagem.")
        return _render_login("senha_esqueci.html", enviado=True,
                             mensagem=MENSAGEM_PEDIDO_SENHA)

    @app.route("/redefinir-senha/<token>", methods=["GET", "POST"])
    def redefinir_senha(token):
        if not dados.link_de_senha_valido(token):
            return _render_login("senha_redefinir.html", invalido=True, status=410)
        if request.method == "POST":
            try:
                dados.redefinir_senha(token, request.form.get("senha") or "",
                                      request.form.get("confirmacao") or "")
            except dados.ErroDeCampo as e:
                return _render_login("senha_redefinir.html", status=400,
                                     erros={e.campo: str(e)})
            except ValueError:
                return _render_login("senha_redefinir.html", invalido=True, status=410)
            session.clear()
            flash("Senha alterada. Entre com a nova senha.", "ok")
            return redirect(url_for("entrar"))
        return _render_login("senha_redefinir.html", minimo=dados.SENHA_MINIMA)

    @app.route("/api/login/config")
    def api_login_config():
        """Configuração pública da página de login (uma chamada, só dados visuais)."""
        c = dados.config_login()
        corpo = {k: c[k] for k in (*dados.CHAVES_LOGIN, "texto_botao")}
        corpo["logo_url"] = url_for("static", filename=c["logo"] or c["logo_padrao"])
        corpo["imagem_url"] = (url_for("static", filename=c["login_imagem"])
                               if c["login_imagem"] else "")
        resposta = jsonify(corpo)
        resposta.headers["Cache-Control"] = "public, max-age=60"
        return resposta

    @app.route("/sair")
    def sair():
        uid = session.get("usuario_id")
        if uid:
            dados.registrar_acao(uid, "logout", "Saiu do sistema")
        auth.logout()
        return redirect(url_for("entrar"))

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _dados_painel(ano: int) -> dict:
        import sqlite3

        def bloco(fn, *args):
            try:
                return fn(*args)
            except sqlite3.Error:
                app.logger.exception("Falha ao carregar bloco do dashboard")
                return None

        rota_esteira = ("painel_operacional" if _pode("painel_operacional")
                        else "lista_pedidos")
        rotas_alerta = {
            "devolucao_atrasada": (rota_esteira, {}),
            "orcamento_sem_retorno": ("lista_orcamentos", {"status": "enviado"}),
        }
        alertas = bloco(dados.alertas_dashboard)
        if alertas is not None:
            alertas = [
                dict(a, link=url_for(rotas_alerta[a["tipo"]][0],
                                     **rotas_alerta[a["tipo"]][1]))
                for a in alertas if _pode(rotas_alerta[a["tipo"]][0])]

        corpo = {
            "usuario_nome": session.get("usuario_nome", ""),
            "indicadores": bloco(dados.indicadores_dashboard),
            "agenda_hoje": bloco(dados.agenda_do_dia),
            "agenda_semana": bloco(dados.agenda_proximos_dias),
            "esteira": bloco(dados.esteira_pedidos, 2),
            "alertas": alertas,
            "link_esteira": url_for(rota_esteira),
        }
        if _pode("faturamento"):
            hoje = formato.agora()
            mensal = bloco(dados.faturamento_mensal,
                           int(hoje[:4]), int(hoje[5:7]))
            corpo["faturamento_mes"] = (
                {k: v for k, v in mensal.items() if k != "registros"}
                if mensal is not None else None)
            meses = bloco(dados.faturamento_anual, ano)
            corpo["faturamento_anual"] = (
                {"ano": ano, "meses": meses} if meses is not None else None)
        return corpo

    def _ano_valido(ano: int | None) -> int:
        atual = int(formato.agora()[:4])
        return ano if ano and 2000 <= ano <= atual + 1 else atual

    @app.route("/")
    @auth.exige_permissao("dashboard.view")
    def painel():
        from datetime import date

        hoje = date.fromisoformat(formato.agora()[:10])
        ano = _ano_valido(request.args.get("ano", type=int))
        return render_template("painel.html", aba="dashboard",
                               hoje=hoje, ano=ano, meses=MESES_PT,
                               anos=sorted({hoje.year - 2, hoje.year - 1,
                                            hoje.year, ano}),
                               **_dados_painel(ano))

    # ------------------------------------------------------------------
    # Usuarios
    # ------------------------------------------------------------------

    @app.route("/usuarios")
    @auth.exige_permissao("users.view")
    def lista_usuarios():
        ver = request.args.get("ver", "ativos")
        if ver == "todos":
            lista = dados.listar_usuarios(somente_ativos=False)
        elif ver == "inativos":
            lista = [u for u in dados.listar_usuarios(False) if not u["ativo"]]
        else:
            lista = dados.listar_usuarios(True)

        busca = request.args.get("q", "")
        lista = listas.filtrar(lista, busca, listas.BUSCA_USUARIOS)

        ordem = request.args.get("ordem", "")
        invertido = request.args.get("dir") == "desc"
        lista = listas.ordenar(lista, ordem, listas.ORDENS_USUARIOS, invertido)

        return render_template("usuarios.html", usuarios=lista,
                               pedidos_senha=(dados.pedidos_de_senha_pendentes()
                                              if auth.tem("users.manage") else []),
                               rotulos_perfil=dados.ROTULOS_PERFIL,
                               abas_config=ABAS_CONFIG,
                               ver=ver, busca=busca, ordem=ordem,
                               invertido=invertido,
                               ordens=listas.ORDENS_USUARIOS)

    @app.route("/usuario", methods=["GET", "POST"])
    @app.route("/usuario/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("users.manage")
    def editar_usuario(id_=None):
        usuario = dados.buscar_usuario(id_) if id_ else None
        if id_ and not usuario:
            abort(404)
        contexto = dict(perfis=dados.PERFIS, rotulos_perfil=dados.ROTULOS_PERFIL,
                        proprio=id_ == session.get("usuario_id"))

        if request.method == "POST":
            campos = dados.campos_usuario(request.form)
            if contexto["proprio"]:  # ninguém tira o próprio acesso
                campos["perfil"], campos["ativo"] = usuario["perfil"], 1
            senha = request.form.get("senha", "").strip()
            senha_hash = generate_password_hash(senha) if senha else None

            try:
                dados.salvar_usuario(campos, senha_hash, id_,
                                     usuario_id=session.get("usuario_id"))
                flash(f"Usuário {'atualizado' if id_ else 'criado'}.", "ok")
                return redirect(url_for("lista_usuarios"))
            except (dados.ErroDeCampo, ValueError) as e:
                return render_template("usuario.html", atual=dict(campos, id=id_),
                                       **contexto, **_erro(e))

        return render_template("usuario.html", atual=usuario or {}, **contexto)

    @app.route("/usuario/<int:id_>/link-senha", methods=["POST"])
    @auth.exige_permissao("users.manage")
    def link_senha_usuario(id_):
        try:
            token = dados.gerar_link_senha(id_, session.get("usuario_id"))
        except ValueError as e:
            flash(str(e), "erro")
            return redirect(url_for("editar_usuario", id_=id_))
        usuario = dados.buscar_usuario(id_)
        return render_template("usuario.html", atual=usuario, perfis=dados.PERFIS,
                               rotulos_perfil=dados.ROTULOS_PERFIL,
                               proprio=id_ == session.get("usuario_id"),
                               link_senha=_link_absoluto("redefinir_senha", token=token),
                               validade=dados.VALIDADE_LINK_SENHA_MINUTOS)

    @app.route("/usuario/<int:id_>/alternar", methods=["POST"])
    @auth.exige_permissao("users.manage")
    def alternar_usuario(id_):
        try:
            ativo = dados.alternar_usuario(id_, session.get("usuario_id"))
            flash(f"Usuário {'ativado' if ativo else 'desativado'}.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        return redirect(url_for("lista_usuarios", ver=request.form.get("ver") or None))

    # ------------------------------------------------------------------
    # Configurações da empresa (Sprint 2.2)
    # ------------------------------------------------------------------

    def _render_config(aba: str, **ctx):
        return render_template("configuracoes.html", aba=aba, abas=ABAS_CONFIG,
                               pode_editar=auth.tem("settings.edit"), **ctx)

    @app.route("/configuracoes")
    @auth.exige_permissao("settings.view")
    def configuracoes():
        return redirect(url_for("config_empresa"))

    @app.route("/configuracoes/empresa", methods=["GET", "POST"])
    @auth.exige_permissao("settings.view", post="settings.edit")
    def config_empresa():
        if request.method == "POST":
            form = request.form
            try:
                dados.salvar_empresa({c: form.get(c, "") for c in dados.CAMPOS_EMPRESA},
                                     session.get("usuario_id"))
                dados.salvar_config(
                    {c: (form.get(c) or "").strip() for c in
                     ("facebook", "site", "horario_funcionamento")},
                    session.get("usuario_id"))
                logo = request.files.get("logo")
                if logo and logo.filename:
                    _salvar_logo(logo)
                flash("Alterações salvas.", "ok")
                return redirect(url_for("config_empresa"))
            except (dados.ErroDeCampo, ValueError) as e:
                return _render_config("empresa", atual=dict(form), **_erro(e))
        atual = dict(dados.empresa_atual())
        for c in ("facebook", "site", "horario_funcionamento"):
            atual[c] = dados.config(c)
        return _render_config("empresa", atual=atual)

    def _gravar_arquivo_empresa(conteudo: bytes, prefixo: str, ext: str) -> str:
        # pasta da própria empresa: arquivos nunca se misturam entre empresas
        pasta = os.path.join(app.static_folder, "uploads", "empresas",
                             str(dados.tenant_atual()))
        os.makedirs(pasta, exist_ok=True)
        marca = formato.agora().replace(":", "").replace("-", "")
        destino = f"{prefixo}-{marca}-{os.urandom(3).hex()}{ext}"
        with open(os.path.join(pasta, destino), "wb") as f:
            f.write(conteudo)
        return f"uploads/empresas/{dados.tenant_atual()}/{destino}"

    def _salvar_logo(arquivo, campo="logo"):
        """Logo da empresa (identidade central: login, topo e documentos)."""
        try:
            conteudo, ext = imagens.preparar_logo(arquivo.read(imagens.LOGO_MAX_BYTES + 1))
        except imagens.ImagemInvalida as e:
            raise dados.ErroDeCampo(campo, f"{e} Use PNG, JPG, WebP ou SVG, até 2 MB.")
        dados.salvar_logo_empresa(_gravar_arquivo_empresa(conteudo, "logo", ext),
                                  session.get("usuario_id"))

    @app.route("/configuracoes/permissoes")
    @auth.exige_permissao("users.view")
    def config_permissoes():
        return _render_config("permissoes", matriz=permissoes.matriz(),
                              perfis=dados.PERFIS, rotulos_perfil=dados.ROTULOS_PERFIL)

    CAMPOS_OPERACAO = ("prazo_preparacao_dias", "prazo_devolucao_dias",
                       "duracao_evento_padrao_horas", "dias_orcamento_sem_retorno",
                       "regras_retirada", "regras_entrega", "regras_conferencia",
                       "formas_pagamento")

    @app.route("/configuracoes/operacao", methods=["GET", "POST"])
    @auth.exige_permissao("settings.view", post="settings.edit")
    def config_operacao():
        if request.method == "POST":
            valores = {c: (request.form.get(c) or "").strip() for c in CAMPOS_OPERACAO}
            for c in ("prazo_preparacao_dias", "prazo_devolucao_dias",
                      "duracao_evento_padrao_horas", "dias_orcamento_sem_retorno"):
                if valores[c] and not (valores[c].isdigit() and int(valores[c]) <= 365):
                    return _render_config(
                        "operacao", atual=valores, servicos=dados.listar_servicos(),
                        erro="Use um número inteiro de 0 a 365.", campo_erro=c)
            dados.salvar_config(valores, session.get("usuario_id"))
            flash("Alterações salvas.", "ok")
            return redirect(url_for("config_operacao"))
        return _render_config("operacao",
                              atual={c: dados.config(c) for c in CAMPOS_OPERACAO},
                              servicos=dados.listar_servicos())

    @app.route("/configuracoes/servicos", methods=["POST"])
    @auth.exige_permissao("settings.edit")
    def config_servicos():
        acao = request.form.get("acao")
        try:
            if acao == "criar":
                dados.salvar_servico(request.form.get("nome"), session.get("usuario_id"))
                flash("Serviço adicionado.", "ok")
            elif acao == "alternar":
                dados.alternar_servico(request.form.get("id", type=int),
                                       session.get("usuario_id"))
            elif acao in ("subir", "descer"):
                dados.mover_servico(request.form.get("id", type=int),
                                    -1 if acao == "subir" else 1)
        except (dados.ErroDeCampo, ValueError) as e:
            flash(str(e), "erro")
        return redirect(url_for("config_operacao") + "#servicos")

    CAMPOS_SISTEMA = ("idioma", "moeda", "fuso_horario", "formato_data",
                      "cor_primaria", "cor_secundaria")

    @app.route("/configuracoes/sistema", methods=["GET", "POST"])
    @auth.exige_permissao("settings.view", post="settings.edit")
    def config_sistema():
        if request.method == "POST":
            f = request.form
            valores = {c: (f.get(c) or "").strip() for c in CAMPOS_SISTEMA}
            validos = (valores["idioma"] in dados.ROTULOS_IDIOMA
                       and valores["moeda"] in dados.ROTULOS_MOEDA
                       and valores["fuso_horario"] in dados.FUSOS_HORARIOS
                       and valores["formato_data"] in dados.ROTULOS_FORMATO_DATA
                       and all(re.fullmatch(r"#[0-9A-Fa-f]{6}", valores[c])
                               for c in ("cor_primaria", "cor_secundaria")))
            if not validos:
                flash("Valor inválido.", "erro")
                return redirect(url_for("config_sistema"))
            dados.salvar_config(valores, session.get("usuario_id"))
            flash("Alterações salvas.", "ok")
            return redirect(url_for("config_sistema"))
        return _render_config("sistema",
                              atual={c: dados.config(c) for c in CAMPOS_SISTEMA},
                              idiomas=dados.ROTULOS_IDIOMA, moedas=dados.ROTULOS_MOEDA,
                              fusos=dados.FUSOS_HORARIOS,
                              formatos=dados.ROTULOS_FORMATO_DATA)

    # ------------------------------------------------------------------
    # Personalização do Login
    # ------------------------------------------------------------------

    CAMPOS_PERSONALIZACAO = (*dados.CORES_LOGIN, "login_layout", "login_modelo",
                             *dados.TEXTOS_LOGIN)

    def _contexto_personalizacao(**ctx):
        c = dados.config_login()
        atual = {k: c[k] for k in CAMPOS_PERSONALIZACAO}
        atual.update(ctx.pop("atual", {}))
        return dict(atual=atual, lg=c, layouts=dados.LAYOUTS_LOGIN,
                    modelos=dados.MODELOS_LOGIN, limites=dados.TEXTOS_LOGIN,
                    abas_config=ABAS_CONFIG, **ctx)

    @app.route("/configuracoes/login", methods=["GET", "POST"])
    @auth.exige_permissao("settings.view", post="settings.edit")
    def config_login():
        fetch = request.headers.get("X-Requested-With") == "fetch"
        if request.method == "POST":
            f = request.form
            valores = {c: f.get(c, "") for c in CAMPOS_PERSONALIZACAO}
            uid = session.get("usuario_id")
            try:
                logo, fundo = request.files.get("logo"), request.files.get("imagem")
                # tudo validado antes de gravar qualquer coisa
                logo_pronta = fundo_pronto = None
                if logo and logo.filename:
                    try:
                        logo_pronta = imagens.preparar_logo(
                            logo.read(imagens.LOGO_MAX_BYTES + 1))
                    except imagens.ImagemInvalida as e:
                        raise dados.ErroDeCampo("logo", f"{e} Use PNG, JPG, WebP ou SVG, até 2 MB.")
                if fundo and fundo.filename:
                    try:
                        fundo_pronto = imagens.preparar_fundo(
                            fundo.read(imagens.FUNDO_MAX_BYTES + 1))
                    except imagens.ImagemInvalida as e:
                        raise dados.ErroDeCampo("imagem", f"{e} Use JPG, PNG ou WebP, até 8 MB.")
                if fundo_pronto:
                    valores["login_imagem"] = _gravar_arquivo_empresa(
                        fundo_pronto[0], "login-fundo", fundo_pronto[1])
                elif f.get("remover_imagem") == "1":
                    valores["login_imagem"] = ""
                dados.salvar_config_login(valores, uid)
                if logo_pronta:
                    dados.salvar_logo_empresa(
                        _gravar_arquivo_empresa(logo_pronta[0], "logo", logo_pronta[1]), uid)
                elif f.get("remover_logo") == "1" and dados.empresa_atual().get("logo"):
                    dados.salvar_logo_empresa("", uid)
            except dados.ErroDeCampo as e:
                if fetch:
                    return jsonify({"ok": False, "mensagem": str(e),
                                    "campo": e.campo}), 400
                return render_template("login_personalizar.html", **_contexto_personalizacao(
                    atual=valores, **_erro(e))), 400
            if fetch:
                return jsonify({"ok": True, "mensagem": "Alterações salvas."})
            flash("Alterações salvas.", "ok")
            return redirect(url_for("config_login"))
        return render_template("login_personalizar.html", **_contexto_personalizacao())

    @app.route("/configuracoes/login/restaurar", methods=["POST"])
    @auth.exige_permissao("settings.edit")
    def restaurar_login():
        dados.restaurar_login(session.get("usuario_id"))
        flash("Login restaurado para o padrão.", "ok")
        return redirect(url_for("config_login"))

    @app.route("/configuracoes/login/previa")
    @auth.exige_permissao("settings.view")
    def previa_login():
        """A mesma página de login, só para a pré-visualização (não envia nada)."""
        return _render_login(previa=True)

    # ------------------------------------------------------------------
    # Clientes
    # ------------------------------------------------------------------

    @app.route("/clientes")
    @auth.exige_permissao("customers.view")
    def lista_clientes():
        ver = request.args.get("ver", "ativos")
        if ver == "todos":
            lista = dados.listar_clientes(somente_ativos=False)
        elif ver == "inativos":
            lista = [c for c in dados.listar_clientes(False) if c["status"] != "ativo"]
        else:
            lista = dados.listar_clientes(True)

        origem = request.args.get("origem", "")
        if origem:
            lista = [c for c in lista if c.get("origem") == origem]

        tag = request.args.get("tag", "")
        if tag:
            lista = [c for c in lista if tag in c.get("tags", [])]

        classif = request.args.get("classificacao", "")
        if classif:
            lista = [c for c in lista if c.get("classificacao") == classif]

        busca = request.args.get("q", "")
        lista = listas.filtrar(lista, busca, listas.BUSCA_CLIENTES)

        ordem = request.args.get("ordem", "")
        invertido = request.args.get("dir") == "desc"
        lista = listas.ordenar(lista, ordem, listas.ORDENS_CLIENTES, invertido)

        todas_tags = _todas_tags_clientes()
        classificacoes = [rotulo for _, rotulo in dados.CLASSIFICACOES_FESTA]

        return render_template("clientes.html", clientes=lista,
                               ver=ver, busca=busca, ordem=ordem,
                               invertido=invertido,
                               ordens=listas.ORDENS_CLIENTES,
                               origens=dados.ORIGENS_CLIENTE,
                               origem_filtro=origem,
                               tag_filtro=tag,
                               classif_filtro=classif,
                               classificacoes=classificacoes,
                               todas_tags=todas_tags)

    def _todas_tags_clientes() -> list:
        return dados.tags_em_uso("clientes")

    @app.route("/cliente", methods=["GET", "POST"])
    @app.route("/cliente/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("customers.view", post="customers.edit")
    def editar_cliente(id_=None):
        cliente = dados.buscar_cliente(id_) if id_ else None
        if id_ and not cliente:
            abort(404)

        if request.method == "POST":
            if not id_ and not auth.tem("customers.create"):
                abort(403)
            campos = dados.campos_cliente(request.form)
            tags_texto = (request.form.get("tags") or "").strip()
            tags = [t.strip() for t in tags_texto.split(",") if t.strip()] if tags_texto else []

            try:
                novo_id = dados.salvar_cliente(campos, id_, tags)
                acao = "alterou" if id_ else "criou"
                dados.registrar_acao(
                    session.get("usuario_id"), f"cliente_{acao}",
                    f"{acao.capitalize()} cliente {campos['nome']}",
                    {"cliente_id": novo_id})
                flash(f"Cliente {'atualizado' if id_ else 'cadastrado'}.", "ok")
                return redirect(url_for("lista_clientes"))
            except dados.ErroDeCampo as e:
                campos["tags"] = tags
                return render_template("cliente.html", atual=campos,
                                       origens=dados.ORIGENS_CLIENTE, **_erro(e))

        historico_evts = []
        pedidos_cli = []
        if cliente and cliente.get("id"):
            historico_evts = dados.eventos_historico_cliente(cliente["id"])
            pedidos_cli = dados.pedidos_cliente(cliente["id"])

        return render_template("cliente.html",
                               atual=cliente or {},
                               origens=dados.ORIGENS_CLIENTE,
                               historico=historico_evts,
                               pedidos_cliente=pedidos_cli)

    @app.route("/clientes/exportar")
    @auth.exige_permissao("customers.export")
    def exportar_clientes():
        ver = request.args.get("ver", "ativos")
        if ver == "todos":
            lista = dados.listar_clientes(somente_ativos=False)
        elif ver == "inativos":
            lista = [c for c in dados.listar_clientes(False) if c["status"] != "ativo"]
        else:
            lista = dados.listar_clientes(True)

        origem = request.args.get("origem", "")
        if origem:
            lista = [c for c in lista if c.get("origem") == origem]

        csv_texto = dados.exportar_clientes_csv(lista)
        return Response(
            csv_texto,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=clientes.csv"})

    # ------------------------------------------------------------------
    # Categorias
    # ------------------------------------------------------------------

    @app.route("/categorias")
    @auth.exige_permissao("catalog.view")
    def lista_categorias():
        return render_template("categorias.html", arvore=dados.categorias_arvore(),
                               categorias=dados.listar_categorias(),
                               contagem=dados.contagem_por_categoria())

    @app.route("/categoria", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def salvar_categoria():
        f = request.form
        nome = (f.get("nome") or "").strip()
        pai_id = f.get("pai_id", type=int) or None
        id_ = f.get("id", type=int) or None
        try:
            dados.salvar_categoria_completa(
                nome, pai_id, id_, descricao=f.get("descricao", ""),
                visivel=f.get("visivel", "1") == "1", slug=f.get("slug", ""),
                usuario_id=session.get("usuario_id"))
            flash(f"Categoria {'atualizada' if id_ else 'criada'}.", "ok")
        except (dados.ErroDeCampo, ValueError) as e:
            flash(str(e), "erro")
        return redirect(url_for("lista_categorias"))

    @app.route("/categoria/<int:id_>/imagem", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def imagem_categoria(id_):
        if not dados.buscar_categoria(id_):
            abort(404)
        if request.form.get("remover") == "1":
            dados.salvar_imagem_categoria(id_, "", session.get("usuario_id"))
            flash("Imagem removida.", "ok")
            return redirect(url_for("lista_categorias"))
        arquivo = request.files.get("imagem")
        if not arquivo or not arquivo.filename:
            flash("Selecione uma imagem.", "erro")
            return redirect(url_for("lista_categorias"))
        try:
            _, mini = imagens.preparar_foto(arquivo.read(imagens.FOTO_MAX_BYTES + 1))
        except imagens.ImagemInvalida as e:
            flash(f"{e} Use JPG, PNG ou WebP.", "erro")
            return redirect(url_for("lista_categorias"))
        pasta = os.path.join(app.static_folder, "uploads", "categorias", str(dados.tenant_atual()))
        os.makedirs(pasta, exist_ok=True)
        nome = f"{id_}-{os.urandom(4).hex()}.webp"
        with open(os.path.join(pasta, nome), "wb") as fh:
            fh.write(mini)
        dados.salvar_imagem_categoria(
            id_, f"uploads/categorias/{dados.tenant_atual()}/{nome}", session.get("usuario_id"))
        flash("Imagem da categoria salva.", "ok")
        return redirect(url_for("lista_categorias"))

    @app.route("/categoria/<int:id_>/excluir", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def excluir_categoria(id_):
        try:
            dados.excluir_categoria(id_)
            dados.registrar_acao(
                session.get("usuario_id"), "categoria_excluiu",
                f"Excluiu categoria {id_}", {"categoria_id": id_})
            flash("Categoria excluída.", "ok")
        except dados.ErroDeCampo as e:
            flash(str(e), "erro")
        return redirect(url_for("lista_categorias"))

    # ------------------------------------------------------------------
    # Catálogo (administração): produtos e kits numa lista só (Sprint 7)
    # ------------------------------------------------------------------

    VER_LEGADO = {"disponiveis": ("ativos", ""), "manutencao": ("todos", "manutencao"),
                  "inativos": ("inativos", ""), "todos": ("todos", ""), "ativos": ("ativos", "")}

    def _lista_catalogo(tipo_padrao=""):
        a = request.args
        aba = a.get("aba") if a.get("aba") in dict(dados.ABAS_CATALOGO) else ""
        status = a.get("status", "") if a.get("status") in (
            *dados.STATUS_PRODUTO, *dados.STATUS_KIT) else ""
        if not aba:
            aba, legado = VER_LEGADO.get(a.get("ver", ""), ("ativos", ""))
            status = status or legado
        tipo = a.get("tipo") if a.get("tipo") in dados.TIPOS_CATALOGO else tipo_padrao
        if tipo_padrao == "kit" and aba in ("todos", "kits"):
            aba = "todos" if a.get("ver") == "todos" else aba
        categoria = a.get("categoria", type=int)

        def preco(nome):
            try:
                return float((a.get(nome) or "").replace(",", ".")) if a.get(nome) else None
            except ValueError:
                return None
        filtros = dict(aba=aba, busca=a.get("q", "").strip(), categoria_id=categoria,
                       tipo=tipo, status=status, preco_min=preco("preco_min"),
                       preco_max=preco("preco_max"),
                       estoque=a.get("estoque", "") if a.get("estoque") in ("com", "sem") else "",
                       tag=a.get("tag", "").strip())
        todos = dados.itens_catalogo_admin()
        base = [i for i in todos if not tipo or i["tipo"] == tipo]
        itens = dados.filtrar_catalogo_admin(base, **filtros)
        return render_template(
            "catalogo_admin.html", itens=itens, filtros=filtros, abas=dados.ABAS_CATALOGO,
            contagens=dados.contagem_abas_catalogo(base), categorias=dados.listar_categorias(),
            todas_tags=sorted({t for i in todos for t in i["tags"]}, key=dados.normalizar_texto),
            tipo_pagina=tipo_padrao or "", filtros_ativos=sum(1 for c in (
                "categoria_id", "status", "preco_min", "preco_max", "estoque", "tag")
                if filtros[c] not in (None, "")) + (1 if tipo and not tipo_padrao else 0))

    @app.route("/produtos")
    @auth.exige_permissao("catalog.view")
    def lista_produtos():
        return _lista_catalogo()

    @app.route("/kits")
    @auth.exige_permissao("catalog.view")
    def lista_kits():
        return _lista_catalogo("kit")

    def _tags_do_form():
        texto = (request.form.get("tags") or "").strip()
        return [t.strip() for t in texto.split(",") if t.strip()]

    def _campos_permitidos(tipo, campos, atual):
        """Estoque/regras e ativação só com a permissão própria (vale no servidor)."""
        if atual:
            if not auth.tem("inventory.edit"):
                for c in dados.CAMPOS_ESTOQUE:
                    if c in atual:
                        campos[c] = atual[c]
            if not auth.tem("catalog.status"):
                campos["status"] = atual["status"]
        else:
            if not auth.tem("inventory.edit") and tipo == "produto":
                campos["quantidade_total"] = 0  # o estoque é informado pelo administrador
            if not auth.tem("catalog.status"):
                campos["status"] = "inativo"
        return campos

    def _contexto_item(tipo, atual, **ctx):
        from datetime import date
        mes = request.args.get("mes", "")
        try:
            ref = date.fromisoformat(mes + "-01") if mes else date.fromisoformat(formato.agora()[:10]).replace(day=1)
        except ValueError:
            ref = date.fromisoformat(formato.agora()[:10]).replace(day=1)
        calendario = pedidos_mes = None
        if atual.get("id"):
            import calendar as cal_mod
            ultimo = cal_mod.monthrange(ref.year, ref.month)[1]
            ini, fim = ref.isoformat(), ref.replace(day=ultimo).isoformat()
            dias = dados.calendario_item(tipo, atual["id"], ini, fim)
            vazias = (ref.weekday() + 1) % 7
            casas = [None] * vazias + dias
            casas += [None] * (-len(casas) % 7)
            calendario = {"semanas": [casas[i:i + 7] for i in range(0, len(casas), 7)],
                          "titulo": f"{MESES_PT[ref.month - 1]} de {ref.year}",
                          "anterior": (ref - timedelta(days=1)).strftime("%Y-%m"),
                          "seguinte": (ref.replace(day=ultimo) + timedelta(days=1)).strftime("%Y-%m"),
                          "hoje": formato.agora()[:10]}
            pedidos_mes = dados.pedidos_do_item(tipo, atual["id"], ini, fim)
        return dict(tipo=tipo, atual=atual, categorias=dados.listar_categorias(),
                    status_opcoes=dados.STATUS_PRODUTO if tipo == "produto" else dados.STATUS_KIT,
                    selos=dados.SELOS, calendario=calendario, pedidos_mes=pedidos_mes,
                    produtos=dados.listar_produtos() if tipo == "kit" else [],
                    disponibilidade=(dados.disponibilidade_kit(atual["id"])
                                     if tipo == "kit" and atual.get("id") else None),
                    pode_estoque=auth.tem("inventory.edit"),
                    pode_status=auth.tem("catalog.status"),
                    aba=request.args.get("aba", "info"), **ctx)

    def _editar_item(tipo, id_):
        buscar = dados.buscar_produto if tipo == "produto" else dados.buscar_kit
        atual = buscar(id_) if id_ else None
        if id_ and not atual:
            abort(404)
        if request.method == "POST":
            campos = (dados.campos_produto if tipo == "produto" else dados.campos_kit)(request.form)
            campos = _campos_permitidos(tipo, campos, atual)
            tags = _tags_do_form()
            salvar = dados.salvar_produto if tipo == "produto" else dados.salvar_kit
            try:
                novo_id = salvar(campos, id_, tags, usuario_id=session.get("usuario_id"))
            except (dados.ErroDeCampo, ValueError) as e:
                return render_template("item_catalogo.html", **_contexto_item(
                    tipo, dict(atual or {}, **campos, tags=tags), **_erro(e)))
            flash(f"{'Produto' if tipo == 'produto' else 'Kit'} "
                  f"{'atualizado' if id_ else 'cadastrado'}.", "ok")
            return redirect(url_for("editar_produto" if tipo == "produto" else "editar_kit",
                                    id_=novo_id, aba=request.form.get("aba_atual") or None))
        return render_template("item_catalogo.html", **_contexto_item(tipo, atual or {}))

    @app.route("/produto", methods=["GET", "POST"])
    @app.route("/produto/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("catalog.edit")
    def editar_produto(id_=None):
        return _editar_item("produto", id_)

    @app.route("/kit", methods=["GET", "POST"])
    @app.route("/kit/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("catalog.edit")
    def editar_kit(id_=None):
        return _editar_item("kit", id_)

    @app.route("/catalogo-admin/<tipo>/<int:id_>/ativo", methods=["POST"])
    @auth.exige_permissao("catalog.status")
    def ativar_item(tipo, id_):
        if tipo not in dados.TIPOS_CATALOGO:
            abort(404)
        ativo = request.form.get("ativo") == "1"
        try:
            novo = dados.definir_ativo(tipo, id_, ativo, session.get("usuario_id"))
            ok, msg = True, ("Ativado." if ativo else "Inativado. Não aparece mais na vitrine.")
        except ValueError as e:
            ok, msg, novo = False, str(e), None
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": ok, "mensagem": msg, "status": novo}), 200 if ok else 409
        flash(msg, "ok" if ok else "erro")
        return redirect(request.referrer or url_for("lista_produtos"))

    def _copiar_arquivo(tipo):
        def copiar(caminho, novo_id):
            import shutil
            origem = os.path.join(app.static_folder, caminho or "")
            if not caminho or not os.path.isfile(origem):
                return ""
            pasta = "produtos" if tipo == "produto" else "kits"
            destino_dir = os.path.join(app.static_folder, "uploads", pasta, str(novo_id))
            os.makedirs(destino_dir, exist_ok=True)
            nome = f"{os.urandom(6).hex()}{os.path.splitext(caminho)[1]}"
            shutil.copyfile(origem, os.path.join(destino_dir, nome))
            return f"uploads/{pasta}/{novo_id}/{nome}"
        return copiar

    @app.route("/catalogo-admin/<tipo>/<int:id_>/duplicar", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def duplicar_item(tipo, id_):
        if tipo not in dados.TIPOS_CATALOGO:
            abort(404)
        try:
            novo = dados.duplicar_item(tipo, id_, session.get("usuario_id"), _copiar_arquivo(tipo))
        except ValueError as e:
            flash(str(e), "erro")
            return redirect(url_for("lista_produtos"))
        flash("Cópia criada (inativa e fora da vitrine). Revise antes de ativar.", "ok")
        return redirect(url_for("editar_produto" if tipo == "produto" else "editar_kit", id_=novo))

    # --- fotos (produto e kit) -------------------------------------------

    def _enviar_fotos(tipo, id_):
        buscar = dados.buscar_produto if tipo == "produto" else dados.buscar_kit
        item = buscar(id_)
        if not item:
            abort(404)
        editar = "editar_produto" if tipo == "produto" else "editar_kit"
        arquivos = [f for f in request.files.getlist("fotos") + request.files.getlist("foto")
                    if f and f.filename]
        if not arquivos:
            flash("Selecione uma ou mais fotos.", "erro")
            return redirect(url_for(editar, id_=id_, aba="imagens"))
        pasta = "produtos" if tipo == "produto" else "kits"
        destino = os.path.join(app.static_folder, "uploads", pasta, str(id_))
        os.makedirs(destino, exist_ok=True)
        salvar = dados.salvar_foto_produto if tipo == "produto" else dados.salvar_foto_kit
        enviadas, erros = 0, []
        for n, arquivo in enumerate(arquivos):
            try:
                grande, mini = imagens.preparar_foto(arquivo.read(imagens.FOTO_MAX_BYTES + 1))
            except imagens.ImagemInvalida as e:
                erros.append(f"{secure_filename(arquivo.filename) or 'arquivo'}: {e}")
                continue
            nome = os.urandom(6).hex()
            for sufixo, conteudo in (("", grande), ("-mini", mini)):
                with open(os.path.join(destino, f"{nome}{sufixo}.webp"), "wb") as f:
                    f.write(conteudo)
            salvar(id_, f"uploads/{pasta}/{id_}/{nome}.webp",
                   principal=request.form.get("principal") == "1" and n == 0,
                   miniatura=f"uploads/{pasta}/{id_}/{nome}-mini.webp")
            enviadas += 1
        if enviadas:
            dados.registrar_acao(session.get("usuario_id"), f"{tipo}_foto",
                                 f"Adicionou {enviadas} foto(s) em {item['nome']}",
                                 {f"{tipo}_id": id_})
            flash(f"{enviadas} foto(s) adicionada(s).", "ok")
        for e in erros:
            flash(e + " Use JPG, PNG ou WebP.", "erro")
        return redirect(url_for(editar, id_=id_, aba="imagens"))

    def _excluir_foto(tipo, id_, foto_id):
        excluir = dados.excluir_foto_produto if tipo == "produto" else dados.excluir_foto_kit
        foto = excluir(foto_id)
        if foto:
            for c in ("arquivo", "miniatura"):
                caminho = os.path.join(app.static_folder, foto.get(c) or "")
                if foto.get(c) and os.path.isfile(caminho):
                    os.remove(caminho)
            dados.registrar_acao(session.get("usuario_id"), f"{tipo}_foto_excluiu",
                                 f"Excluiu foto do {tipo} {id_}", {f"{tipo}_id": id_})
            flash("Foto excluída.", "ok")
        return redirect(url_for("editar_produto" if tipo == "produto" else "editar_kit",
                                id_=id_, aba="imagens"))

    @app.route("/produto/<int:id_>/foto", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def upload_foto(id_):
        return _enviar_fotos("produto", id_)

    @app.route("/produto/<int:id_>/foto/<int:foto_id>/excluir", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def excluir_foto(id_, foto_id):
        return _excluir_foto("produto", id_, foto_id)

    @app.route("/produto/<int:id_>/foto/<int:foto_id>/principal", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def definir_capa(id_, foto_id):
        dados.definir_foto_principal(foto_id, id_)
        flash("Foto de capa definida.", "ok")
        return redirect(url_for("editar_produto", id_=id_, aba="imagens"))

    @app.route("/kit/<int:id_>/foto", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def upload_foto_kit(id_):
        return _enviar_fotos("kit", id_)

    @app.route("/kit/<int:id_>/foto/<int:foto_id>/excluir", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def excluir_foto_kit(id_, foto_id):
        return _excluir_foto("kit", id_, foto_id)

    @app.route("/kit/<int:id_>/foto/<int:foto_id>/principal", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def definir_capa_kit(id_, foto_id):
        dados.definir_foto_principal_kit(foto_id, id_)
        flash("Foto de capa definida.", "ok")
        return redirect(url_for("editar_kit", id_=id_, aba="imagens"))

    @app.route("/catalogo-admin/<tipo>/<int:id_>/foto/<int:foto_id>/mover", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def mover_foto(tipo, id_, foto_id):
        if tipo not in dados.TIPOS_CATALOGO:
            abort(404)
        dados.mover_foto(tipo, id_, foto_id, 1 if request.form.get("direcao") == "depois" else -1)
        return redirect(url_for("editar_produto" if tipo == "produto" else "editar_kit",
                                id_=id_, aba="imagens"))

    # --- componentes do kit ------------------------------------------------

    @app.route("/kit/<int:id_>/item", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def adicionar_item_kit(id_):
        kit = dados.buscar_kit(id_)
        if not kit:
            abort(404)
        try:
            produto_id = int(request.form.get("produto_id") or 0)
            dados.adicionar_item_kit(id_, produto_id, request.form.get("quantidade", "1"),
                                     usuario_id=session.get("usuario_id"))
            flash("Componente salvo no kit.", "ok")
        except (dados.ErroDeCampo, ValueError) as e:
            flash(str(e) if isinstance(e, dados.ErroDeCampo) else "Produto e quantidade inválidos.",
                  "erro")
        return redirect(url_for("editar_kit", id_=id_, aba="componentes"))

    @app.route("/kit/<int:id_>/item/<int:item_id>/remover", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def remover_item_kit(id_, item_id):
        dados.remover_item_kit(item_id, id_, usuario_id=session.get("usuario_id"))
        flash("Componente removido do kit.", "ok")
        return redirect(url_for("editar_kit", id_=id_, aba="componentes"))

    # ------------------------------------------------------------------
    # Catalogo publico
    # ------------------------------------------------------------------

    # A vitrine só lê o cadastro e o estoque; pedir orçamento não reserva nada.
    _limite_orcamento: dict = {}
    _limite_trava = threading.Lock()
    LIMITE_ORCAMENTOS, JANELA_ORCAMENTOS = 5, 600  # por IP a cada 10 minutos

    def _vitrine(titulo, itens_todos, *, categoria=None, pagina_tipo="", destaques=None,
                 categorias=None, meta_descricao=""):
        pag = dados.paginar(itens_todos, request.args.get("pagina", 1, type=int))
        return render_template(
            "catalogo.html", pag=pag, titulo=titulo, categoria=categoria,
            pagina_tipo=pagina_tipo, destaques=destaques or [],
            categorias=categorias if categorias is not None else dados.vitrine_categorias(),
            busca=request.args.get("q", "").strip()[:80],
            cat_filtro=request.args.get("categoria", ""), org=dados.organizacao(),
            meta_descricao=meta_descricao, eh_inicio=request.endpoint == "catalogo"
            and not request.args.get("q") and not request.args.get("categoria"))

    @app.route("/catalogo")
    def catalogo():
        busca = request.args.get("q", "").strip()[:80]
        cat_id = request.args.get("categoria", type=int)
        itens = dados.vitrine_itens(categoria_id=cat_id, busca=busca)
        inicio = not busca and not cat_id
        destaques = [i for i in itens if i["destaque"] or i["selo"]][:8] if inicio else []
        titulo = (f'Resultados para "{busca}"' if busca else
                  next((c["nome"] for c in dados.listar_categorias() if c["id"] == cat_id),
                       "Catálogo") if cat_id else "Todo o catálogo")
        return _vitrine(titulo, itens, destaques=destaques)

    @app.route("/catalogo/kits")
    def catalogo_kits():
        busca = request.args.get("q", "").strip()[:80]
        return _vitrine("Kits completos", dados.vitrine_itens("kit", busca=busca),
                        pagina_tipo="kit",
                        meta_descricao="Kits de decoração completos, prontos para montar.")

    @app.route("/catalogo/pecas")
    def catalogo_pecas():
        busca = request.args.get("q", "").strip()[:80]
        return _vitrine("Peças avulsas", dados.vitrine_itens("produto", busca=busca),
                        pagina_tipo="produto",
                        meta_descricao="Peças de decoração para locação, uma a uma.")

    @app.route("/catalogo/produto/<int:id_>")
    def catalogo_produto(id_):
        item = dados.produto_publico(id_)
        if not item or not item.get("slug"):
            abort(404)
        return redirect(url_for("catalogo_item", slug=item["slug"]), 301)

    @app.route("/catalogo/kit/<int:id_>")
    def catalogo_kit(id_):
        item = dados.kit_publico(id_)
        if not item or not item.get("slug"):
            abort(404)
        return redirect(url_for("catalogo_item", slug=item["slug"]), 301)

    @app.route("/catalogo/<slug>")
    def catalogo_item(slug):
        achado = dados.publico_por_slug(slug)
        if not achado:
            abort(404)
        tipo, item = achado
        if tipo == "categoria":
            busca = request.args.get("q", "").strip()[:80]
            return _vitrine(item["nome"], dados.vitrine_itens(categoria_id=item["id"], busca=busca),
                            categoria=item, meta_descricao=item.get("descricao") or "")
        relacionados = [i for i in dados.vitrine_itens(categoria_id=item.get("categoria_id"))
                        if not (i["tipo"] == tipo and i["id"] == item["id"])][:4] \
            if item.get("categoria_id") else []
        return render_template("catalogo_detalhe.html", tipo=tipo, item=item,
                               org=dados.organizacao(), relacionados=relacionados,
                               hoje=formato.agora()[:10],
                               data_escolhida=request.args.get("data", ""))

    def _dados_json():
        corpo = request.get_json(silent=True)
        return corpo if isinstance(corpo, dict) else {}

    @app.route("/catalogo/api/disponibilidade")
    def catalogo_api_disponibilidade():
        """Situação de um item da vitrine numa faixa de datas (máx. 62 dias)."""
        from datetime import date as _date
        a = request.args
        tipo, id_ = a.get("tipo", ""), a.get("id", type=int)
        inicio = a.get("inicio") or a.get("data") or ""
        fim = a.get("fim") or inicio
        qtd = min(max(a.get("qtd", 1, type=int) or 1, 1), dados.ORCAMENTO_MAX_QTD)
        try:
            d_ini, d_fim = _date.fromisoformat(inicio), _date.fromisoformat(fim)
        except ValueError:
            return jsonify({"erro": "Data inválida."}), 400
        if tipo not in dados.TIPOS_CATALOGO or not id_ or d_fim < d_ini \
                or (d_fim - d_ini).days > 62:
            return jsonify({"erro": "Consulta inválida."}), 400
        with dados.conectar() as conn:
            publico = dados._item_publico(conn, tipo, id_)
        if not publico:
            return jsonify({"erro": "Item não encontrado."}), 404
        dias = dados.calendario_item(tipo, id_, inicio, fim, qtd)
        # para o público: situação e rótulo; nada de estoque exato nem de pedidos
        return jsonify({"dias": [{"data": d["data"], "situacao": d["situacao"],
                                  "rotulo": d["rotulo"], "motivo": d["motivo"]} for d in dias]})

    @app.route("/catalogo/api/resumo", methods=["POST"])
    def catalogo_api_resumo():
        corpo = _dados_json()
        try:
            resumo = dados.resumo_orcamento_publico(corpo.get("itens") or [],
                                                    str(corpo.get("data") or "")[:10])
        except ValueError:
            return jsonify({"erro": "Data inválida."}), 400
        for ln in resumo["itens"]:
            ln.pop("livres", None)
            ln["url"] = url_for("catalogo_item", slug=ln["slug"])
            ln["capa"] = url_for("static", filename=ln["capa"]) if ln.get("capa") else ""
        return jsonify(resumo)

    def _ip_cliente():
        return request.remote_addr or "?"

    def _pode_enviar_orcamento() -> bool:
        agora_ = time.monotonic()
        with _limite_trava:
            for ip in [ip for ip, ts in _limite_orcamento.items()
                       if not ts or agora_ - ts[-1] > JANELA_ORCAMENTOS]:
                _limite_orcamento.pop(ip, None)
            envios = [t for t in _limite_orcamento.get(_ip_cliente(), [])
                      if agora_ - t < JANELA_ORCAMENTOS]
            if len(envios) >= LIMITE_ORCAMENTOS:
                return False
            envios.append(agora_)
            _limite_orcamento[_ip_cliente()] = envios
            return True

    @app.route("/catalogo/orcamento", methods=["GET", "POST"])
    def catalogo_orcamento():
        if request.method == "GET":
            return render_template("catalogo_orcamento.html", org=dados.organizacao(),
                                   hoje=formato.agora()[:10])
        if not request.is_json:  # JSON exige a mesma origem (pré-verificação do navegador)
            return jsonify({"ok": False, "mensagem": "Envio inválido."}), 400
        corpo = _dados_json()
        if str(corpo.get("site") or "").strip():  # campo-armadilha: robôs preenchem
            return jsonify({"ok": True, "mensagem": "Pedido recebido."})
        if not _pode_enviar_orcamento():
            return jsonify({"ok": False, "mensagem": "Muitos envios seguidos. Tente de novo em alguns minutos."}), 429
        try:
            r = dados.solicitar_orcamento_catalogo(
                corpo.get("contato") if isinstance(corpo.get("contato"), dict) else {},
                corpo.get("itens") or [], str(corpo.get("data") or "")[:10])
        except dados.ErroDeCampo as e:
            return jsonify({"ok": False, "campo": e.campo, "mensagem": str(e)}), 422
        org = dados.organizacao()
        texto = (f"Olá! Acabei de pedir um orçamento pelo site (nº {r['orcamento_id']}) "
                 f"para a festa em {formato.fmt_data(str(corpo.get('data'))[:10])}.")
        wpp = formato.whatsapp_link(org.get("whatsapp")) if org else ""
        return jsonify({"ok": True, "numero": r["orcamento_id"], "total": r["total"],
                        "mensagem": "Pedido recebido! Nossa equipe vai responder pelo WhatsApp.",
                        "whatsapp": f"{wpp}?text={quote(texto)}" if wpp else ""})

    # ------------------------------------------------------------------
    # Catalogo interno
    # ------------------------------------------------------------------

    @app.route("/catalogo-interno")
    @auth.exige_permissao("catalog.view")
    def catalogo_interno():
        busca = request.args.get("q", "")
        cat_id = request.args.get("categoria", "")
        cat_id_int = None
        if cat_id:
            try:
                cat_id_int = int(cat_id)
            except (TypeError, ValueError):
                cat_id = ""

        produtos = dados.listar_produtos("disponivel")
        if cat_id_int:
            produtos = [p for p in produtos if p.get("categoria_id") == cat_id_int]
        kits = dados.listar_kits("ativo")

        if busca:
            from sistema.listas import filtrar, BUSCA_PRODUTOS, BUSCA_KITS
            produtos = filtrar(produtos, busca, BUSCA_PRODUTOS)
            kits = filtrar(kits, busca, BUSCA_KITS)

        return render_template("catalogo_interno.html",
                               produtos=produtos,
                               kits=kits,
                               categorias=dados.listar_categorias(),
                               busca=busca,
                               cat_filtro=cat_id)

    # ------------------------------------------------------------------
    # Origens de lead
    # ------------------------------------------------------------------

    @app.route("/origens")
    @auth.exige_permissao("settings.view")
    def lista_origens():
        return render_template("origens.html",
                               origens=dados.listar_origens())

    @app.route("/origem", methods=["POST"])
    @auth.exige_permissao("settings.edit")
    def salvar_origem_rota():
        try:
            nome = request.form.get("nome", "")
            id_ = request.form.get("id")
            id_ = int(id_) if id_ else None
            dados.salvar_origem(nome, id_)
            flash("Origem salva.", "ok")
        except (dados.ErroDeCampo, ValueError) as e:
            flash(str(e), "erro")
        return redirect(url_for("lista_origens"))

    @app.route("/origem/<int:id_>/excluir", methods=["POST"])
    @auth.exige_permissao("settings.edit")
    def excluir_origem_rota(id_):
        try:
            dados.excluir_origem(id_)
            flash("Origem excluída.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        return redirect(url_for("lista_origens"))

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------

    @app.route("/leads")
    @auth.exige_permissao("leads.view")
    def lista_leads():
        modo = request.args.get("modo", "kanban")
        busca = request.args.get("q", "")
        origem_filtro = request.args.get("origem", "")

        if modo == "kanban":
            funil = dados.leads_por_etapa()
            contadores = dados.contadores_lead()
            return render_template("leads_kanban.html",
                                   funil=funil,
                                   contadores=contadores,
                                   etapas=dados.ETAPAS_LEAD,
                                   origens=dados.listar_origens(),
                                   modo=modo)

        todos = dados.listar_leads()
        if busca:
            from sistema.listas import filtrar, BUSCA_LEADS
            todos = filtrar(todos, busca, BUSCA_LEADS)
        if origem_filtro:
            todos = [l for l in todos if l.get("origem_nome") == origem_filtro]
        return render_template("leads.html",
                               leads=todos,
                               origens=dados.listar_origens(),
                               busca=busca,
                               origem_filtro=origem_filtro,
                               modo=modo)

    @app.route("/lead", methods=["GET", "POST"])
    @app.route("/lead/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("leads.view", post="leads.edit")
    def editar_lead(id_=None):
        atual = dados.buscar_lead(id_) if id_ else {}
        if id_ and not atual:
            abort(404)

        if request.method == "POST":
            try:
                c = dados.campos_lead(request.form)
                novo_id = dados.salvar_lead(c, id_)
                dados.registrar_acao(
                    session.get("usuario_id"), "lead",
                    f"{'Editou' if id_ else 'Criou'} lead #{novo_id}")
                flash("Lead salvo.", "ok")
                return redirect(url_for("editar_lead", id_=novo_id))
            except (dados.ErroDeCampo, ValueError) as e:
                atual = dados.campos_lead(request.form)
                return render_template("lead.html", atual=atual,
                                       clientes=dados.listar_clientes(),
                                       origens=dados.listar_origens(),
                                       usuarios=dados.listar_usuarios(),
                                       etapas=dados.ETAPAS_LEAD,
                                       etapas_alt=dados.ETAPAS_ALT_LEAD,
                                       **_erro(e))

        return render_template("lead.html", atual=atual,
                               clientes=dados.listar_clientes(),
                               origens=dados.listar_origens(),
                               usuarios=dados.listar_usuarios(),
                               etapas=dados.ETAPAS_LEAD,
                               etapas_alt=dados.ETAPAS_ALT_LEAD)

    @app.route("/lead/<int:id_>/mover", methods=["POST"])
    @auth.exige_permissao("leads.edit")
    def mover_lead_rota(id_):
        novo = request.form.get("status", "")
        try:
            dados.mover_lead(id_, novo)
        except ValueError as e:
            flash(str(e), "erro")
        ref = request.form.get("retorno", "")
        if ref == "kanban":
            return redirect(url_for("lista_leads", modo="kanban"))
        return redirect(url_for("editar_lead", id_=id_))

    # ------------------------------------------------------------------
    # Orcamentos
    # ------------------------------------------------------------------

    @app.route("/orcamentos")
    @auth.exige_permissao("quotes.view")
    def lista_orcamentos():
        busca = request.args.get("q", "")
        status_f = request.args.get("status", "")
        orcs = dados.listar_orcamentos(status_f or None)
        if busca:
            from sistema.listas import filtrar, BUSCA_ORCAMENTOS
            orcs = filtrar(orcs, busca, BUSCA_ORCAMENTOS)
        return render_template("orcamentos.html",
                               orcamentos=orcs,
                               busca=busca,
                               status_filtro=status_f,
                               status_opcoes=dados.STATUS_ORCAMENTO)

    @app.route("/orcamento", methods=["GET", "POST"])
    @app.route("/orcamento/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("quotes.view", post="quotes.edit")
    def editar_orcamento(id_=None):
        atual = dados.buscar_orcamento(id_) if id_ else {}
        if id_ and not atual:
            abort(404)

        if request.method == "POST":
            try:
                d = {
                    "cliente_id": int(request.form.get("cliente_id") or 0) or None,
                    "lead_id": int(request.form.get("lead_id") or 0) or None,
                    "desconto": request.form.get("desconto", "0"),
                    "observacoes": request.form.get("observacoes", ""),
                    "status": request.form.get("status", "rascunho"),
                    "data_evento": (request.form.get("data_evento") or "").strip() or None,
                }
                itens = []
                i = 0
                while True:
                    desc = request.form.get(f"item_descricao_{i}")
                    if desc is None:
                        break
                    itens.append({
                        "tipo": request.form.get(f"item_tipo_{i}", "produto"),
                        "item_id": int(request.form.get(f"item_item_id_{i}") or 0) or None,
                        "descricao": desc,
                        "quantidade": request.form.get(f"item_quantidade_{i}", "1"),
                        "preco_unitario": request.form.get(f"item_preco_{i}", "0"),
                    })
                    i += 1
                novo_id = dados.salvar_orcamento(d, itens, id_)
                dados.registrar_acao(
                    session.get("usuario_id"), "orcamento",
                    f"{'Editou' if id_ else 'Criou'} orçamento #{novo_id}")
                flash("Orçamento salvo.", "ok")
                return redirect(url_for("editar_orcamento", id_=novo_id))
            except (dados.ErroDeCampo, ValueError) as e:
                flash(str(e), "erro")
                return redirect(url_for("editar_orcamento", id_=id_) if id_
                                else url_for("editar_orcamento"))

        return render_template("orcamento.html", atual=atual,
                               clientes=dados.listar_clientes(),
                               leads=dados.listar_leads(),
                               produtos=dados.listar_produtos("disponivel"),
                               kits=dados.listar_kits("ativo"),
                               status_opcoes=dados.STATUS_ORCAMENTO)

    @app.route("/orcamento/<int:id_>/converter", methods=["POST"])
    @auth.exige_permissao("quotes.edit")
    def converter_orcamento(id_):
        try:
            pedido_id = dados.converter_orcamento_em_pedido(
                id_, session.get("usuario_id"))
            dados.registrar_acao(
                session.get("usuario_id"), "pedido",
                f"Converteu orçamento #{id_} em pedido #{pedido_id}")
            flash(f"Pedido #{pedido_id} criado a partir do orçamento.", "ok")
            return redirect(url_for("ver_pedido", id_=pedido_id))
        except ValueError as e:
            flash(str(e), "erro")
            return redirect(url_for("editar_orcamento", id_=id_))

    # ------------------------------------------------------------------
    # Pedidos
    # ------------------------------------------------------------------

    @app.route("/pedidos")
    @auth.exige_permissao("orders.view")
    def lista_pedidos():
        from datetime import date
        hoje = date.fromisoformat(formato.agora()[:10])
        a = request.args
        filtros = {
            "q": a.get("q", "").strip(),
            "status_comercial": a.get("status_comercial", ""),
            "status_operacional": a.get("status_operacional", ""),
            "origem": a.get("origem", ""),
            "canal": a.get("canal", ""),
            "periodo": a.get("periodo", ""),
            "inicio": a.get("inicio", ""),
            "fim": a.get("fim", ""),
        }
        erro_periodo = None
        inicio = fim = None
        if filtros["periodo"] in dict(dados.PERIODOS_PEDIDOS):
            try:
                inicio, fim = dados.intervalo_periodo(
                    filtros["periodo"], hoje, filtros["inicio"], filtros["fim"])
            except ValueError as e:
                erro_periodo = str(e)
        else:
            filtros["periodo"] = ""
        res = dados.consultar_pedidos(
            q=filtros["q"], status_comercial=filtros["status_comercial"],
            status_operacional=filtros["status_operacional"],
            inicio=inicio, fim=fim, origem=filtros["origem"],
            canal=filtros["canal"] if filtros["canal"] in dados.CANAIS else "",
            aba=a.get("aba", "todos"), pagina=a.get("pagina", 1, type=int),
            por_pagina=a.get("por_pagina", 10, type=int),
            ordem=a.get("ordem", "evento"), direcao=a.get("dir", "desc"))
        def url_lista(**mudancas):
            args = {k: v for k, v in a.items() if k != "parcial"}
            args.update(mudancas)
            return url_for("lista_pedidos",
                           **{k: v for k, v in args.items() if v not in (None, "")})

        contexto = dict(
            res=res, filtros=filtros, erro_periodo=erro_periodo, url_lista=url_lista,
            ordem=a.get("ordem", "evento"), direcao=a.get("dir", "desc"),
            abas=dados.ABAS_PEDIDOS, periodos=dados.PERIODOS_PEDIDOS,
            origens=dados.origens_pedidos_unificados(), canais=dados.CANAIS,
            status_comercial_opcoes=dados.STATUS_PEDIDO_COMERCIAL,
            status_operacional_opcoes=dados.STATUS_PEDIDO_OPERACIONAL,
            rot_com=dados.ROTULOS_COMERCIAL, rot_op=dados.ROTULOS_OPERACIONAL,
            por_pagina_opcoes=dados.POR_PAGINA_PEDIDOS)
        if a.get("parcial") == "1":
            return render_template("_pedidos_resultado.html", **contexto)
        return render_template("pedidos.html", **contexto)

    @app.route("/pedido/<int:id_>")
    @auth.exige_permissao("orders.view")
    def ver_pedido(id_):
        ped = dados.buscar_pedido_detalhe(id_)
        if not ped:
            abort(404)
        return render_template("pedido_festas.html", pedido=ped,
                               rot_com=dados.ROTULOS_COMERCIAL,
                               rot_op=dados.ROTULOS_OPERACIONAL)

    @app.route("/pedido/historico/<int:id_>")
    @auth.exige_permissao("orders.view")
    def ver_pedido_historico(id_):
        # importado que já virou pedido atual: o pedido é a única fonte
        pedido_id = dados.pedido_do_historico(id_)
        if pedido_id:
            return redirect(url_for("ver_pedido", id_=pedido_id))
        ped = dados.buscar_historico_detalhe(id_)
        if not ped:
            abort(404)
        return render_template("pedido_festas.html", pedido=ped,
                               rot_com=dados.ROTULOS_COMERCIAL,
                               rot_op=dados.ROTULOS_OPERACIONAL)

    @app.route("/pedido/historico/<int:id_>/converter", methods=["POST"])
    @auth.exige_permissao("orders.admin")
    def converter_historico(id_):
        try:
            pedido_id = dados.converter_historico_em_pedido(
                id_, session.get("usuario_id"))
        except ValueError as e:
            flash(str(e), "erro")
            return redirect(url_for("ver_pedido_historico", id_=id_))
        flash("Festa importada convertida em pedido atual.", "ok")
        return redirect(url_for("ver_pedido", id_=pedido_id))

    def _voltar_pedido(id_):
        voltar = request.form.get("voltar") or ""
        if voltar.startswith(("/pedido", "/operacao")) and not voltar.startswith("//"):
            return redirect(voltar)
        return redirect(url_for("ver_pedido", id_=id_))

    @app.route("/pedido/novo", methods=["GET", "POST"])
    @app.route("/pedido/<int:id_>/editar", methods=["GET", "POST"])
    @auth.exige_permissao("orders.edit")
    def editar_pedido(id_=None):
        if not id_ and not auth.tem("orders.create"):
            abort(403)
        atual = dados.buscar_pedido_festas(id_) if id_ else {}
        if id_ and not atual:
            abort(404)
        pode_alterar_status = auth.tem("orders.admin")

        if request.method == "POST":
            try:
                d = dados.campos_pedido_festas(request.form)
                indices = sorted(int(k.rsplit("_", 1)[1]) for k in request.form
                                 if re.fullmatch(r"item_descricao_\d+", k))
                itens = []
                for i in indices:
                    itens.append({
                        "tipo": request.form.get(f"item_tipo_{i}", "produto"),
                        "item_id": int(request.form.get(f"item_item_id_{i}") or 0) or None,
                        "descricao": request.form.get(f"item_descricao_{i}"),
                        "quantidade": request.form.get(f"item_quantidade_{i}", "1"),
                        "preco_unitario": request.form.get(f"item_preco_{i}", "0"),
                    })
                novo_id = dados.salvar_pedido_festas(
                    d, itens, id_, usuario_id=session.get("usuario_id"),
                    pode_alterar_status=pode_alterar_status)
                flash("Pedido salvo.", "ok")
                return redirect(url_for("ver_pedido", id_=novo_id))
            except (dados.ErroDeCampo, ValueError) as e:
                flash(str(e), "erro")
                return redirect(url_for("editar_pedido", id_=id_) if id_
                                else url_for("editar_pedido"))

        return render_template("pedido.html", atual=atual,
                               clientes=dados.listar_clientes(),
                               pode_alterar_status=pode_alterar_status,
                               status_comercial=dados.STATUS_PEDIDO_COMERCIAL,
                               status_operacional=dados.STATUS_PEDIDO_OPERACIONAL,
                               rot_com=dados.ROTULOS_COMERCIAL,
                               rot_op=dados.ROTULOS_OPERACIONAL,
                               canais=dados.CANAIS,
                               responsaveis=dados.opcoes_responsavel(atual),
                               servicos=dados.listar_servicos(somente_ativos=True),
                               formas_pagamento=dados.lista_config("formas_pagamento"))

    @app.route("/pedido/<int:id_>/cancelar", methods=["POST"])
    @auth.exige_permissao("orders.cancel")
    def cancelar_pedido_rota(id_):
        motivo = (request.form.get("motivo") or "").strip()
        try:
            dados.cancelar_pedido(id_, motivo, session.get("usuario_id"))
            flash("Pedido cancelado.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        return _voltar_pedido(id_)

    @app.route("/pedido/<int:id_>/finalizar", methods=["POST"])
    @auth.exige_permissao("orders.finish")
    def finalizar_pedido_rota(id_):
        try:
            dados.finalizar_pedido(id_, session.get("usuario_id"))
            flash("Pedido finalizado.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        return _voltar_pedido(id_)

    @app.route("/pedido/<int:id_>/ocorrencia", methods=["POST"])
    @auth.exige_permissao("orders.note")
    def ocorrencia_pedido(id_):
        try:
            dados.registrar_ocorrencia(id_, request.form.get("texto"),
                                       session.get("usuario_id"),
                                       tipo=request.form.get("tipo") or "")
            flash("Ocorrência registrada.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        return _voltar_pedido(id_)

    @app.route("/faturamento")
    @auth.exige_permissao("reports.view")
    def faturamento():
        periodo, fat = _faturamento_da_requisicao()
        so_sem_valor = request.args.get("sem_valor") == "1"
        registros = [r for r in fat["registros"]
                     if not so_sem_valor or r["valor"] is None]
        return render_template("faturamento.html", fat=fat, periodo=periodo,
                               periodos=dados.PERIODOS_FATURAMENTO,
                               registros=registros, so_sem_valor=so_sem_valor,
                               datas_futuras=dados.historicos_data_futura(),
                               origens_editaveis_bloqueadas=dados.ORIGENS_SOMENTE_LEITURA)

    @app.route("/faturamento/historico/<int:id_>", methods=["POST"])
    @auth.exige_permissao("finance.edit")
    def salvar_historico(id_):
        voltar = request.form.get("voltar") or ""
        if not voltar.startswith("/faturamento"):
            voltar = url_for("faturamento")
        try:
            r = dados.salvar_historico_manual(
                id_,
                valor=formato.ler_dinheiro(request.form.get("valor")),
                data_evento=request.form.get("data_evento") or None,
                usuario_id=session.get("usuario_id"))
            if r["alterado"]:
                flash("Alterações salvas.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        return redirect(voltar)

    def _faturamento_da_requisicao():
        from datetime import date
        hoje = date.fromisoformat(formato.agora()[:10])
        periodo = request.args.get("periodo", "")
        inicio = request.args.get("inicio", "")
        fim = request.args.get("fim", "")
        ano = request.args.get("ano", type=int)
        mes = request.args.get("mes", type=int)
        if not periodo and ano and mes and 1 <= mes <= 12:
            periodo = "personalizado"
            inicio, fim = dados._intervalo_mes(ano, mes)
        if periodo not in dict(dados.PERIODOS_FATURAMENTO):
            periodo = "este_mes"
        try:
            inicio, fim = dados.intervalo_periodo(periodo, hoje, inicio, fim)
        except ValueError as e:
            flash(str(e), "erro")
            periodo = "este_mes"
            inicio, fim = dados.intervalo_periodo(periodo, hoje)
        return periodo, dados.faturamento_periodo(inicio, fim)

    # ------------------------------------------------------------------
    # Operacao — painel e transicoes
    # ------------------------------------------------------------------

    @app.route("/operacao")
    @auth.exige_permissao("operation.view")
    def painel_operacional():
        """Esteira de pedidos (Sprint 3): visão operacional dos próprios pedidos."""
        from datetime import date, timedelta
        a = request.args
        hoje = formato.agora()[:10]
        try:
            dia = date.fromisoformat(a.get("data", "")).isoformat()
        except ValueError:
            dia = hoje
        filtros = {
            "evento": a.get("evento", "") if a.get("evento") in dict(dados.FILTROS_EVENTO) else "",
            "servico": a.get("servico", "").strip(),
            "responsavel": a.get("responsavel", "").strip(),
            "status": a.get("status", "") if a.get("status") in dados.COLUNA_DO_STATUS.values() else "",
            "q": a.get("q", "").strip(),
        }
        quadro = dados.quadro_esteira(dia, filtros)
        d = date.fromisoformat(dia)

        def url_esteira(**mudancas):
            args = {k: v for k, v in a.items() if k not in ("parcial", "destaque")}
            args.update(mudancas)
            if args.get("data") == hoje:
                args.pop("data")
            return url_for("painel_operacional",
                           **{k: v for k, v in args.items() if v not in (None, "")})

        contexto = dict(
            quadro=quadro, filtros=filtros, dia=dia, hoje=hoje, eh_hoje=dia == hoje,
            dia_anterior=(d - timedelta(days=1)).isoformat(),
            dia_seguinte=(d + timedelta(days=1)).isoformat(),
            filtros_evento=dados.FILTROS_EVENTO, etapas=dados.ETAPAS_OPERACAO,
            url_esteira=url_esteira, destaque=a.get("destaque", type=int),
            pode_mover=auth.tem("operation.edit"),
            pode_finalizar=auth.tem("orders.finish"),
            dias_finalizados=dados.DIAS_FINALIZADOS_NA_ESTEIRA)
        if a.get("parcial") == "1":
            return render_template("_esteira_quadro.html", **contexto)
        return render_template("esteira.html", **contexto)

    @app.route("/operacao/pedido/<int:id_>")
    @auth.exige_permissao("operation.view")
    def ver_pedido_operacional(id_):
        # um só detalhe de pedido: o da tela de Pedidos (timeline completa)
        return redirect(url_for("ver_pedido", id_=id_))

    @app.route("/operacao/pedido/<int:id_>/mover", methods=["POST"])
    @auth.exige_permissao("operation.view")
    def mover_pedido(id_):
        """Muda a etapa (botão do cartão ou arrastar): mesma validação sempre."""
        destino = request.form.get("destino", "")
        necessaria = "orders.finish" if destino == "finalizado" else "operation.edit"
        if not auth.tem(necessaria):
            abort(403)
        try:
            novo = dados.mover_etapa(id_, destino, session.get("usuario_id"),
                                     esperado=request.form.get("esperado") or None,
                                     observacao=request.form.get("observacao") or "")
            ok, mensagem = True, (f"Pedido #{id_} movido para "
                                  f"{dados.ROTULOS_OPERACIONAL.get(novo, novo)}.")
        except ValueError as e:
            ok, mensagem = False, str(e)
        if request.headers.get("X-Requested-With") == "fetch":
            from flask import jsonify
            return jsonify({"ok": ok, "mensagem": mensagem}), 200 if ok else 409
        flash(mensagem, "ok" if ok else "erro")
        voltar = request.form.get("voltar") or ""
        if voltar.startswith("/operacao") and not voltar.startswith("//"):
            return redirect(voltar)
        return redirect(url_for("painel_operacional"))

    @app.route("/operacao/pedido/<int:id_>/avancar", methods=["POST"])
    @auth.exige_permissao("operation.edit")
    def avancar_pedido(id_):
        obs = (request.form.get("observacao") or "").strip()
        try:
            novo = dados.avancar_status_operacional(id_, obs, session.get("usuario_id"))
            flash(f"Pedido avançou para {dados.ROTULOS_OPERACIONAL.get(novo, novo).lower()}.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        voltar = request.form.get("voltar") or ""
        if voltar.startswith("/pedido") and not voltar.startswith("//"):
            return redirect(voltar)
        return redirect(url_for("ver_pedido", id_=id_))

    # ------------------------------------------------------------------
    # Agenda
    # ------------------------------------------------------------------

    MESES_PT = [
        "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
    ]

    VISOES_AGENDA = {"mensal": "Mensal", "semanal": "Semanal", "diaria": "Diária"}
    SEMANA_CURTA = ("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")  # por weekday()
    MAX_NO_DIA_MENSAL = 3  # compromissos visíveis por casa; o resto vira "+N mais"

    @app.route("/agenda")
    @auth.exige_permissao("agenda.view")
    def agenda():
        """Agenda (Sprint 4): projeção dos pedidos no tempo; nada é gravado aqui."""
        import calendar as cal_mod
        from datetime import date, timedelta
        a = request.args
        hoje = date.fromisoformat(formato.agora()[:10])
        visao = a.get("visao") if a.get("visao") in VISOES_AGENDA else "mensal"
        try:
            ref = date.fromisoformat(a.get("data", ""))
        except ValueError:
            # links antigos: ano/mes/dia
            try:
                ref = date(a.get("ano", type=int) or hoje.year,
                           a.get("mes", type=int) or hoje.month,
                           a.get("dia", type=int) or (1 if a.get("mes") else hoje.day))
            except ValueError:
                ref = hoje
        if not 2000 <= ref.year <= 2100:
            ref = hoje
        filtros = dados.filtros_agenda(a)

        if visao == "mensal":
            inicio = ref.replace(day=1)
            fim = ref.replace(day=cal_mod.monthrange(ref.year, ref.month)[1])
            # mês vizinho, no mesmo dia (ou no último dia, se o mês for menor)
            anterior, seguinte = (
                d.replace(day=min(ref.day, cal_mod.monthrange(d.year, d.month)[1]))
                for d in (inicio - timedelta(days=1), fim + timedelta(days=1)))
        elif visao == "semanal":
            inicio = ref - timedelta(days=(ref.weekday() + 1) % 7)  # domingo
            fim = inicio + timedelta(days=6)
            anterior, seguinte = ref - timedelta(days=7), ref + timedelta(days=7)
        else:
            inicio = fim = ref
            anterior, seguinte = ref - timedelta(days=1), ref + timedelta(days=1)

        agenda_ = dados.agenda_periodo(inicio.isoformat(), fim.isoformat(), filtros)
        dias = [(inicio + timedelta(days=i)).isoformat()
                for i in range((fim - inicio).days + 1)]
        # grade mensal de domingo a sábado, com casas vazias fora do mês
        vazias = (inicio.weekday() + 1) % 7
        casas = [None] * vazias + dias
        casas += [None] * (-len(casas) % 7)
        semanas = [casas[i:i + 7] for i in range(0, len(casas), 7)]

        def url_agenda(**mudancas):
            args = {k: v for k, v in a.items()
                    if k not in ("parcial", "destaque", "ano", "mes", "dia")}
            args.setdefault("data", ref.isoformat())
            args.update(mudancas)
            if args.get("visao") == "mensal":
                args.pop("visao")
            return url_for("agenda", **{k: v for k, v in args.items() if v not in (None, "")})

        def rotulo_dia(iso, ano=False):
            d = date.fromisoformat(iso)
            texto = f"{SEMANA_CURTA[d.weekday()]}, {d.day} de {MESES_PT[d.month - 1]}"
            return f"{texto} de {d.year}" if ano else texto

        if visao == "mensal":
            titulo = f"{MESES_PT[ref.month - 1]} {ref.year}"
        elif visao == "semanal":
            mes_i, mes_f = MESES_PT[inicio.month - 1][:3].lower(), MESES_PT[fim.month - 1][:3].lower()
            titulo = (f"{inicio.day} – {fim.day} {mes_f} {fim.year}" if mes_i == mes_f
                      else f"{inicio.day} {mes_i} – {fim.day} {mes_f} {fim.year}")
        else:
            titulo = rotulo_dia(ref.isoformat(), ano=True)

        contexto = dict(
            titulo_periodo=titulo, rotulo_dia=rotulo_dia, semana_curta=SEMANA_CURTA,
            agenda=agenda_, visao=visao, visoes=VISOES_AGENDA, filtros=filtros,
            ref=ref.isoformat(), hoje=hoje.isoformat(), dias=dias, semanas=semanas,
            inicio=inicio.isoformat(), fim=fim.isoformat(),
            anterior=anterior.isoformat(), seguinte=seguinte.isoformat(),
            tipos=dados.TIPOS_AGENDA, status_agenda=dados.STATUS_AGENDA,
            destaque=a.get("destaque", type=int), url_agenda=url_agenda,
            max_no_dia=MAX_NO_DIA_MENSAL)
        if a.get("parcial") == "1":
            return render_template("_agenda_conteudo.html", **contexto)
        return render_template("agenda.html", **contexto)

    @app.route("/disponibilidade/<int:id_>")
    @auth.exige_permissao("inventory.view")
    def disponibilidade_produto(id_):
        import calendar as cal_mod
        from datetime import date

        produto = dados.buscar_produto(id_)
        if not produto:
            abort(404)

        ano = request.args.get("ano", type=int) or date.fromisoformat(formato.agora()[:10]).year
        mes = request.args.get("mes", type=int) or date.fromisoformat(formato.agora()[:10]).month
        if mes < 1 or mes > 12:
            mes = date.fromisoformat(formato.agora()[:10]).month

        calendario = dados.disponibilidade_calendario(id_, ano, mes)
        _, _ = cal_mod.monthrange(ano, mes)
        primeiro_dia_semana = cal_mod.weekday(ano, mes, 1)
        offset_dia = (primeiro_dia_semana + 1) % 7

        if mes == 1:
            ano_ant, mes_ant = ano - 1, 12
        else:
            ano_ant, mes_ant = ano, mes - 1
        if mes == 12:
            ano_prox, mes_prox = ano + 1, 1
        else:
            ano_prox, mes_prox = ano, mes + 1

        return render_template("disponibilidade.html",
                               produto=produto,
                               calendario=calendario,
                               ano=ano, mes=mes,
                               ano_ant=ano_ant, mes_ant=mes_ant,
                               ano_prox=ano_prox, mes_prox=mes_prox,
                               offset_dia=offset_dia,
                               meses=MESES_PT)

    @app.route("/api/disponibilidade")
    @auth.exige_permissao("inventory.view")
    def api_disponibilidade():
        from flask import jsonify
        produto_id = request.args.get("produto_id", type=int)
        data_inicio = request.args.get("data_inicio", "")
        data_fim = request.args.get("data_fim", "")
        if not produto_id:
            return jsonify({"erro": "produto_id obrigatório"}), 400
        disp = dados.disponibilidade(produto_id, data_inicio or None,
                                     data_fim or None)
        return jsonify({"disponivel": disp})

    @app.route("/api/faturamento")
    @auth.exige_permissao("reports.view")
    def api_faturamento():
        from flask import jsonify
        periodo, fat = _faturamento_da_requisicao()
        return jsonify(dict(fat, periodo=periodo))

    @app.route("/api/painel")
    @auth.exige_permissao("dashboard.view")
    def api_painel():
        from flask import jsonify
        return jsonify(_dados_painel(
            _ano_valido(request.args.get("ano", type=int))))

    return app
