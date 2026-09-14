"""Aplicacao Flask — Morumbi Festas."""

import os

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import generate_password_hash

from sistema import auth, dados, formato, listas


MENU = (
    ("Painel", (
        ("painel", "Dashboard"),
    )),
    ("Administracao", (
        ("lista_usuarios", "Usuarios"),
    )),
)


def _grupo_de(endpoint: str) -> str:
    for grupo, itens in MENU:
        for ep, _ in itens:
            if ep == endpoint:
                return grupo
    return ""


def _erro(exc):
    campo = getattr(exc, "campo", None)
    return {"erro": str(exc), "campo_erro": campo}


def criar_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder="static")
    app.secret_key = os.environ.get("FESTAS_SECRET", "dev-morumbi-festas-2026")

    formato.registrar(app)
    dados.inicializar()
    auth.seed_admin()

    @app.context_processor
    def contexto_global():
        u = auth.usuario_atual()
        return {
            "perfil": session.get("perfil", ""),
            "usuario_nome": session.get("usuario_nome", ""),
            "usuario_id": session.get("usuario_id"),
            "com_senha": auth.com_senha(),
            "menu": MENU,
            "grupo_ativo": _grupo_de(request.endpoint or ""),
        }

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    @app.route("/entrar", methods=["GET", "POST"])
    def entrar():
        if request.method == "POST":
            login_ = (request.form.get("login") or "").strip().lower()
            senha = request.form.get("senha") or ""
            u = auth.login(login_, senha)
            if u:
                dados.registrar_acao(u["id"], "login", f"{u['nome']} entrou")
                return redirect(url_for("painel"))
            flash("Login ou senha incorretos.", "erro")
        return render_template("login.html")

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

    @app.route("/")
    @auth.exige_login
    def painel():
        return render_template("painel.html", aba="dashboard",
                               **dados.resumo_painel())

    # ------------------------------------------------------------------
    # Usuarios
    # ------------------------------------------------------------------

    @app.route("/usuarios")
    @auth.exige_perfil("admin")
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
                               ver=ver, busca=busca, ordem=ordem,
                               invertido=invertido,
                               ordens=listas.ORDENS_USUARIOS)

    @app.route("/usuario", methods=["GET", "POST"])
    @app.route("/usuario/<int:id_>", methods=["GET", "POST"])
    @auth.exige_perfil("admin")
    def editar_usuario(id_=None):
        usuario = dados.buscar_usuario(id_) if id_ else None
        if id_ and not usuario:
            abort(404)

        if request.method == "POST":
            campos = dados.campos_usuario(request.form)
            senha = request.form.get("senha", "").strip()
            senha_hash = generate_password_hash(senha) if senha else None

            try:
                novo_id = dados.salvar_usuario(campos, senha_hash, id_)
                acao = "alterou" if id_ else "criou"
                dados.registrar_acao(
                    session.get("usuario_id"), f"usuario_{acao}",
                    f"{acao.capitalize()} usuario {campos['nome']}",
                    {"usuario_id": novo_id})
                flash(f"Usuario {'atualizado' if id_ else 'criado'}.", "ok")
                return redirect(url_for("lista_usuarios"))
            except dados.ErroDeCampo as e:
                return render_template("usuario.html", atual=campos,
                                       perfis=dados.PERFIS, **_erro(e))

        return render_template("usuario.html",
                               atual=usuario or {},
                               perfis=dados.PERFIS)

    return app
