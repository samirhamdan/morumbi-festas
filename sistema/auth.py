"""Autenticação, empresa atual e autorização por permissão (Sprint 2.2).

Fluxo de cada requisição: usuário autenticado → vínculo (membro) com a
empresa da sessão → permissões do perfil nesse vínculo. A empresa nunca vem
de um campo enviado pela tela: ela é gravada na sessão (assinada) no login,
a partir dos vínculos do próprio usuário, e revalidada a cada requisição.
"""

import functools
import os

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from sistema import dados, permissoes


class AcessoNegado(Exception):
    """Senha correta, mas sem empresa ativa para entrar."""


def com_senha() -> bool:
    return bool(os.environ.get("FESTAS_SENHA"))


def _sem_senha_padrao():
    if not com_senha():
        session.setdefault("usuario_id", 1)


def membro_atual() -> dict | None:
    """Usuário + vínculo com a empresa da sessão (None se não puder entrar)."""
    if "_membro" in g:
        return g._membro
    m = None
    uid = session.get("usuario_id")
    if uid:
        tid = session.get("tenant_id")
        if not tid:  # sessão aberta antes das empresas: usa o primeiro vínculo
            vinculos = dados.membros_do_usuario(uid)
            tid = vinculos[0]["tenant_id"] if vinculos else None
        m = dados.membro(uid, tid) if tid else None
        if m:
            session["tenant_id"] = m["tenant_id"]
            session["perfil"] = m["perfil"]
    g._membro = m
    return m


def _usuario_logado() -> dict | None:
    return membro_atual()


def _recusar():
    if session.get("usuario_id"):
        session.clear()
        flash("Seu acesso foi encerrado. Entre novamente.", "erro")
    return redirect(url_for("entrar"))


def permissoes_atuais() -> frozenset:
    m = membro_atual()
    return permissoes.do_perfil(m["perfil"]) if m else frozenset()


def tem(permissao: str) -> bool:
    return permissao in permissoes_atuais()


def exige_login(f):
    @functools.wraps(f)
    def decorada(*args, **kwargs):
        _sem_senha_padrao()
        if not membro_atual():
            return _recusar()
        return f(*args, **kwargs)
    return decorada


def exige_permissao(permissao: str, post: str | None = None):
    """Rota protegida por permissão; `post` exige outra permissão para gravar."""
    def decorador(f):
        @functools.wraps(f)
        def decorada(*args, **kwargs):
            _sem_senha_padrao()
            if not membro_atual():
                return _recusar()
            necessaria = post if (post and request.method == "POST") else permissao
            if not tem(necessaria):
                abort(403)
            return f(*args, **kwargs)
        decorada.permissao = permissao
        return decorada
    return decorador


def pode_acessar(endpoint: str, view_functions) -> bool:
    view = view_functions.get(endpoint)
    permissao = getattr(view, "permissao", None)
    return permissao is None or tem(permissao)


def login(login_: str, senha: str) -> dict | None:
    u = dados.buscar_usuario_por_login(login_)
    if not u or not u["ativo"]:
        return None
    if not check_password_hash(u["senha_hash"], senha):
        return None
    vinculos = dados.membros_do_usuario(u["id"])
    if not vinculos:
        raise AcessoNegado("Seu acesso está desativado ou a empresa está"
                           " suspensa. Fale com o administrador.")
    # Hoje há uma empresa por usuário; a escolha entre várias fica para depois.
    m = vinculos[0]
    session.clear()
    session["usuario_id"] = u["id"]
    session["tenant_id"] = m["tenant_id"]
    session["perfil"] = m["perfil"]
    session["usuario_nome"] = u["nome"]
    g.pop("_membro", None)
    dados.registrar_acesso(u["id"], m["tenant_id"])
    return dict(u, tenant_id=m["tenant_id"], perfil=m["perfil"])


def logout():
    session.clear()
    g.pop("_membro", None)


def usuario_atual() -> dict | None:
    return membro_atual()


def seed_admin():
    """Cria o administrador inicial da empresa principal num banco novo."""
    if dados.existe_usuario():
        return
    senha = os.environ.get("FESTAS_SENHA", "admin")
    dados.salvar_usuario(
        {"nome": "Administrador", "login": "admin", "perfil": "admin"},
        senha_hash=generate_password_hash(senha),
    )
