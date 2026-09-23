"""Autenticacao e controle de perfil."""

import functools
import os

from flask import redirect, request, session, url_for, abort
from werkzeug.security import check_password_hash, generate_password_hash

from sistema import dados


def com_senha() -> bool:
    return bool(os.environ.get("FESTAS_SENHA"))


def _usuario_logado() -> dict | None:
    uid = session.get("usuario_id")
    if not uid:
        return None
    u = dados.buscar_usuario(uid)
    if u and u["ativo"]:
        return u
    return None


def exige_login(f):
    @functools.wraps(f)
    def decorada(*args, **kwargs):
        if not com_senha():
            session.setdefault("usuario_id", 1)
            session.setdefault("perfil", "admin")
        u = _usuario_logado()
        if not u:
            return redirect(url_for("entrar"))
        return f(*args, **kwargs)
    return decorada


def exige_perfil(*perfis):
    def decorador(f):
        @functools.wraps(f)
        def decorada(*args, **kwargs):
            if not com_senha():
                session.setdefault("usuario_id", 1)
                session.setdefault("perfil", "admin")
            u = _usuario_logado()
            if not u:
                return redirect(url_for("entrar"))
            if u["perfil"] not in perfis:
                abort(403)
            return f(*args, **kwargs)
        decorada.perfis = perfis
        return decorada
    return decorador


def pode_acessar(endpoint: str, perfil: str | None, view_functions) -> bool:
    view = view_functions.get(endpoint)
    perfis = getattr(view, "perfis", None)
    return perfis is None or perfil in perfis


def login(login_: str, senha: str) -> dict | None:
    u = dados.buscar_usuario_por_login(login_)
    if not u or not u["ativo"]:
        return None
    if not check_password_hash(u["senha_hash"], senha):
        return None
    session["usuario_id"] = u["id"]
    session["perfil"] = u["perfil"]
    session["usuario_nome"] = u["nome"]
    return u


def logout():
    session.clear()


def usuario_atual() -> dict | None:
    return _usuario_logado()


def seed_admin():
    """Cria admin padrao se nao existir nenhum usuario."""
    usuarios = dados.listar_usuarios(somente_ativos=False)
    if usuarios:
        return
    senha = os.environ.get("FESTAS_SENHA", "admin")
    dados.salvar_usuario(
        {"nome": "Administrador", "login": "admin", "perfil": "admin"},
        senha_hash=generate_password_hash(senha),
    )
