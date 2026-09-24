"""Aplicacao Flask — Morumbi Festas."""

import io
import os
import re

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from sistema import auth, dados, formato, listas, permissoes

UPLOAD_EXTENSOES = {".jpg", ".jpeg", ".png", ".webp"}
UPLOAD_MAX_MB = 10


MENU = (
    ("Painel", (
        ("painel", "Dashboard"),
    )),
    ("Comercial", (
        ("lista_leads", "Leads"),
        ("lista_orcamentos", "Orçamentos"),
        ("lista_pedidos", "Pedidos"),
        ("lista_clientes", "Clientes"),
    )),
    ("Operação", (
        ("agenda", "Agenda"),
        ("painel_operacional", "Esteira de pedidos"),
    )),
    ("Catálogo", (
        ("lista_produtos", "Produtos"),
        ("lista_kits", "Kits"),
        ("lista_categorias", "Categorias"),
        ("catalogo_interno", "Vitrine"),
    )),
    ("BI", (
        ("faturamento", "Relatórios"),
    )),
    ("Configurações", (
        ("config_empresa", "Empresa"),
        ("lista_usuarios", "Usuários"),
        ("config_permissoes", "Permissões"),
        ("config_operacao", "Operação"),
        ("config_sistema", "Sistema"),
        ("lista_origens", "Origens de lead"),
    )),
)

# Abas da área de Configurações (endpoint, rótulo).
ABAS_CONFIG = (
    ("config_empresa", "Empresa"), ("lista_usuarios", "Usuários"),
    ("config_permissoes", "Permissões"), ("config_operacao", "Operação"),
    ("config_sistema", "Sistema"),
)
LOGO_MAX_BYTES = 2 * 1024 * 1024


def _grupo_de(endpoint: str) -> str:
    for grupo, itens in MENU:
        for ep, _ in itens:
            if ep == endpoint:
                return grupo
    return ""


def _erro(exc):
    campo = getattr(exc, "campo", None)
    return {"erro": str(exc), "campo_erro": campo}


def _eventos_do_dia(eventos: list, data_str: str,
                    historico: list | None = None) -> list:
    resultado = []
    for e in eventos:
        tipos = []
        if e.get("data_evento") == data_str:
            tipos.append("evento")
        if e.get("data_retirada") == data_str:
            tipos.append("retirada")
        if e.get("data_devolucao") == data_str:
            tipos.append("devolucao")
        if tipos:
            resultado.append({**e, "tipos_dia": tipos})
    for h in (historico or []):
        if h.get("data_evento") == data_str:
            resultado.append({**h, "tipos_dia": ["historico"],
                              "historico": True})
    return resultado


def criar_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder="static")
    app.secret_key = os.environ.get("FESTAS_SECRET", "dev-morumbi-festas-2026")

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
            "grupo_ativo": _grupo_de(request.endpoint or ""),
            "css_ver": _css_ver,
            "pode": _pode,
            "rotulo_fonte": dados.rotulo_fonte,
            "tipos_ocorrencia": dados.TIPOS_OCORRENCIA,
        }

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    @app.route("/entrar", methods=["GET", "POST"])
    def entrar():
        if request.method == "POST":
            login_ = (request.form.get("login") or "").strip().lower()
            senha = request.form.get("senha") or ""
            try:
                u = auth.login(login_, senha)
            except auth.AcessoNegado as e:
                flash(str(e), "erro")
                return render_template("login.html")
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

    def _salvar_logo(arquivo):
        nome = secure_filename(arquivo.filename)
        _, ext = os.path.splitext(nome)
        if ext.lower() not in UPLOAD_EXTENSOES:
            raise dados.ErroDeCampo("logo", "Formato inválido. Use JPG, PNG ou WebP.")
        conteudo = arquivo.read(LOGO_MAX_BYTES + 1)
        if len(conteudo) > LOGO_MAX_BYTES:
            raise dados.ErroDeCampo("logo", "O logo deve ter no máximo 2 MB.")
        # pasta da própria empresa: arquivos nunca se misturam entre empresas
        pasta = os.path.join(app.static_folder, "uploads", "empresas",
                             str(dados.tenant_atual()))
        os.makedirs(pasta, exist_ok=True)
        destino = f"logo-{formato.agora().replace(':', '').replace('-', '')}{ext.lower()}"
        with open(os.path.join(pasta, destino), "wb") as f:
            f.write(conteudo)
        dados.salvar_logo_empresa(
            f"uploads/empresas/{dados.tenant_atual()}/{destino}", session.get("usuario_id"))

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
        arvore = dados.categorias_arvore()
        return render_template("categorias.html", arvore=arvore,
                               categorias=dados.listar_categorias())

    @app.route("/categoria", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def salvar_categoria():
        nome = (request.form.get("nome") or "").strip()
        pai_id = request.form.get("pai_id") or None
        if pai_id:
            try:
                pai_id = int(pai_id)
            except (TypeError, ValueError):
                pai_id = None
        id_ = request.form.get("id") or None
        if id_:
            try:
                id_ = int(id_)
            except (TypeError, ValueError):
                id_ = None

        try:
            novo_id = dados.salvar_categoria(nome, pai_id, id_)
            acao = "alterou" if id_ else "criou"
            dados.registrar_acao(
                session.get("usuario_id"), f"categoria_{acao}",
                f"{acao.capitalize()} categoria {nome}",
                {"categoria_id": novo_id})
            flash(f"Categoria {'atualizada' if id_ else 'criada'}.", "ok")
        except dados.ErroDeCampo as e:
            flash(str(e), "erro")

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
    # Produtos
    # ------------------------------------------------------------------

    @app.route("/produtos")
    @auth.exige_permissao("catalog.view")
    def lista_produtos():
        ver = request.args.get("ver", "disponiveis")
        if ver == "todos":
            lista = dados.listar_produtos()
        elif ver == "manutencao":
            lista = dados.listar_produtos("manutencao")
        elif ver == "inativos":
            lista = dados.listar_produtos("inativo")
        else:
            lista = dados.listar_produtos("disponivel")

        cat_id = request.args.get("categoria", "")
        if cat_id:
            try:
                cat_id_int = int(cat_id)
                lista = [p for p in lista if p.get("categoria_id") == cat_id_int]
            except (TypeError, ValueError):
                cat_id = ""

        tag = request.args.get("tag", "")
        if tag:
            lista = [p for p in lista if tag in p.get("tags", [])]

        busca = request.args.get("q", "")
        lista = listas.filtrar(lista, busca, listas.BUSCA_PRODUTOS)

        ordem = request.args.get("ordem", "")
        invertido = request.args.get("dir") == "desc"
        lista = listas.ordenar(lista, ordem, listas.ORDENS_PRODUTOS, invertido)

        todas_tags = _todas_tags_produtos()

        return render_template("produtos.html", produtos=lista,
                               ver=ver, busca=busca, ordem=ordem,
                               invertido=invertido,
                               ordens=listas.ORDENS_PRODUTOS,
                               categorias=dados.listar_categorias(),
                               cat_filtro=cat_id,
                               tag_filtro=tag,
                               todas_tags=todas_tags)

    def _todas_tags_produtos() -> list:
        return dados.tags_em_uso("produtos")

    @app.route("/produto", methods=["GET", "POST"])
    @app.route("/produto/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("catalog.edit")
    def editar_produto(id_=None):
        produto = dados.buscar_produto(id_) if id_ else None
        if id_ and not produto:
            abort(404)

        if request.method == "POST":
            campos = dados.campos_produto(request.form)
            tags_texto = (request.form.get("tags") or "").strip()
            tags = [t.strip() for t in tags_texto.split(",") if t.strip()] if tags_texto else []

            try:
                novo_id = dados.salvar_produto(campos, id_, tags)
                acao = "alterou" if id_ else "criou"
                dados.registrar_acao(
                    session.get("usuario_id"), f"produto_{acao}",
                    f"{acao.capitalize()} produto {campos['nome']}",
                    {"produto_id": novo_id})
                flash(f"Produto {'atualizado' if id_ else 'cadastrado'}.", "ok")
                return redirect(url_for("editar_produto", id_=novo_id))
            except dados.ErroDeCampo as e:
                campos["tags"] = tags
                return render_template("produto.html", atual=campos,
                                       categorias=dados.listar_categorias(),
                                       status_opcoes=dados.STATUS_PRODUTO,
                                       **_erro(e))

        return render_template("produto.html",
                               atual=produto or {},
                               categorias=dados.listar_categorias(),
                               status_opcoes=dados.STATUS_PRODUTO)

    def _pasta_fotos(produto_id: int) -> str:
        pasta = os.path.join(app.static_folder, "uploads", "produtos",
                             str(produto_id))
        os.makedirs(pasta, exist_ok=True)
        return pasta

    @app.route("/produto/<int:id_>/foto", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def upload_foto(id_):
        produto = dados.buscar_produto(id_)
        if not produto:
            abort(404)

        arquivo = request.files.get("foto")
        if not arquivo or not arquivo.filename:
            flash("Selecione uma foto.", "erro")
            return redirect(url_for("editar_produto", id_=id_))

        nome_seguro = secure_filename(arquivo.filename)
        _, ext = os.path.splitext(nome_seguro)
        if ext.lower() not in UPLOAD_EXTENSOES:
            flash("Formato inválido. Use JPG, PNG ou WebP.", "erro")
            return redirect(url_for("editar_produto", id_=id_))

        pasta = _pasta_fotos(id_)
        caminho = os.path.join(pasta, nome_seguro)
        arquivo.save(caminho)

        try:
            from PIL import Image
            img = Image.open(caminho)
            if max(img.size) > 1200:
                img.thumbnail((1200, 1200), Image.LANCZOS)
                img.save(caminho)
        except ImportError:
            pass

        principal = request.form.get("principal") == "1"
        caminho_rel = f"uploads/produtos/{id_}/{nome_seguro}"
        dados.salvar_foto_produto(id_, caminho_rel, principal)

        dados.registrar_acao(
            session.get("usuario_id"), "produto_foto",
            f"Adicionou foto ao produto {produto['nome']}",
            {"produto_id": id_})
        flash("Foto adicionada.", "ok")
        return redirect(url_for("editar_produto", id_=id_))

    @app.route("/produto/<int:id_>/foto/<int:foto_id>/excluir", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def excluir_foto(id_, foto_id):
        foto = dados.excluir_foto_produto(foto_id)
        if foto:
            caminho = os.path.join(app.static_folder, foto["arquivo"])
            if os.path.exists(caminho):
                os.remove(caminho)
            dados.registrar_acao(
                session.get("usuario_id"), "produto_foto_excluiu",
                f"Excluiu foto do produto {id_}",
                {"produto_id": id_})
            flash("Foto excluída.", "ok")
        return redirect(url_for("editar_produto", id_=id_))

    @app.route("/produto/<int:id_>/foto/<int:foto_id>/principal", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def definir_capa(id_, foto_id):
        dados.definir_foto_principal(foto_id, id_)
        flash("Foto de capa definida.", "ok")
        return redirect(url_for("editar_produto", id_=id_))

    # ------------------------------------------------------------------
    # Kits
    # ------------------------------------------------------------------

    @app.route("/kits")
    @auth.exige_permissao("catalog.view")
    def lista_kits():
        ver = request.args.get("ver", "ativos")
        if ver == "todos":
            lista = dados.listar_kits()
        elif ver == "inativos":
            lista = dados.listar_kits("inativo")
        else:
            lista = dados.listar_kits("ativo")

        busca = request.args.get("q", "")
        lista = listas.filtrar(lista, busca, listas.BUSCA_KITS)

        ordem = request.args.get("ordem", "")
        invertido = request.args.get("dir") == "desc"
        lista = listas.ordenar(lista, ordem, listas.ORDENS_KITS, invertido)

        return render_template("kits.html", kits=lista,
                               ver=ver, busca=busca, ordem=ordem,
                               invertido=invertido,
                               ordens=listas.ORDENS_KITS)

    @app.route("/kit", methods=["GET", "POST"])
    @app.route("/kit/<int:id_>", methods=["GET", "POST"])
    @auth.exige_permissao("catalog.edit")
    def editar_kit(id_=None):
        kit = dados.buscar_kit(id_) if id_ else None
        if id_ and not kit:
            abort(404)

        if request.method == "POST":
            campos = dados.campos_kit(request.form)
            try:
                novo_id = dados.salvar_kit(campos, id_)
                acao = "alterou" if id_ else "criou"
                dados.registrar_acao(
                    session.get("usuario_id"), f"kit_{acao}",
                    f"{acao.capitalize()} kit {campos['nome']}",
                    {"kit_id": novo_id})
                flash(f"Kit {'atualizado' if id_ else 'cadastrado'}.", "ok")
                return redirect(url_for("editar_kit", id_=novo_id))
            except dados.ErroDeCampo as e:
                return render_template("kit.html", atual=campos,
                                       produtos=dados.listar_produtos("disponivel"),
                                       status_opcoes=dados.STATUS_KIT,
                                       **_erro(e))

        disp = None
        if kit:
            disp = dados.disponibilidade_kit(id_)

        return render_template("kit.html",
                               atual=kit or {},
                               produtos=dados.listar_produtos("disponivel"),
                               status_opcoes=dados.STATUS_KIT,
                               disponibilidade=disp)

    @app.route("/kit/<int:id_>/item", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def adicionar_item_kit(id_):
        kit = dados.buscar_kit(id_)
        if not kit:
            abort(404)

        produto_id = request.form.get("produto_id")
        quantidade = request.form.get("quantidade", "1")
        try:
            produto_id = int(produto_id)
            quantidade = int(quantidade)
        except (TypeError, ValueError):
            flash("Produto e quantidade inválidos.", "erro")
            return redirect(url_for("editar_kit", id_=id_))

        try:
            dados.adicionar_item_kit(id_, produto_id, quantidade)
            dados.registrar_acao(
                session.get("usuario_id"), "kit_item_adicionou",
                f"Adicionou item ao kit {kit['nome']}",
                {"kit_id": id_, "produto_id": produto_id})
            flash("Produto adicionado ao kit.", "ok")
        except dados.ErroDeCampo as e:
            flash(str(e), "erro")

        return redirect(url_for("editar_kit", id_=id_))

    @app.route("/kit/<int:id_>/item/<int:item_id>/remover", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def remover_item_kit(id_, item_id):
        dados.remover_item_kit(item_id, id_)
        dados.registrar_acao(
            session.get("usuario_id"), "kit_item_removeu",
            f"Removeu item do kit {id_}",
            {"kit_id": id_, "item_id": item_id})
        flash("Produto removido do kit.", "ok")
        return redirect(url_for("editar_kit", id_=id_))

    def _pasta_fotos_kit(kit_id: int) -> str:
        pasta = os.path.join(app.static_folder, "uploads", "kits",
                             str(kit_id))
        os.makedirs(pasta, exist_ok=True)
        return pasta

    @app.route("/kit/<int:id_>/foto", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def upload_foto_kit(id_):
        kit = dados.buscar_kit(id_)
        if not kit:
            abort(404)

        arquivo = request.files.get("foto")
        if not arquivo or not arquivo.filename:
            flash("Selecione uma foto.", "erro")
            return redirect(url_for("editar_kit", id_=id_))

        nome_seguro = secure_filename(arquivo.filename)
        _, ext = os.path.splitext(nome_seguro)
        if ext.lower() not in UPLOAD_EXTENSOES:
            flash("Formato inválido. Use JPG, PNG ou WebP.", "erro")
            return redirect(url_for("editar_kit", id_=id_))

        pasta = _pasta_fotos_kit(id_)
        caminho = os.path.join(pasta, nome_seguro)
        arquivo.save(caminho)

        try:
            from PIL import Image
            img = Image.open(caminho)
            if max(img.size) > 1200:
                img.thumbnail((1200, 1200), Image.LANCZOS)
                img.save(caminho)
        except ImportError:
            pass

        principal = request.form.get("principal") == "1"
        caminho_rel = f"uploads/kits/{id_}/{nome_seguro}"
        dados.salvar_foto_kit(id_, caminho_rel, principal)

        dados.registrar_acao(
            session.get("usuario_id"), "kit_foto",
            f"Adicionou foto ao kit {kit['nome']}",
            {"kit_id": id_})
        flash("Foto adicionada.", "ok")
        return redirect(url_for("editar_kit", id_=id_))

    @app.route("/kit/<int:id_>/foto/<int:foto_id>/excluir", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def excluir_foto_kit(id_, foto_id):
        foto = dados.excluir_foto_kit(foto_id)
        if foto:
            caminho = os.path.join(app.static_folder, foto["arquivo"])
            if os.path.exists(caminho):
                os.remove(caminho)
            dados.registrar_acao(
                session.get("usuario_id"), "kit_foto_excluiu",
                f"Excluiu foto do kit {id_}",
                {"kit_id": id_})
            flash("Foto excluída.", "ok")
        return redirect(url_for("editar_kit", id_=id_))

    @app.route("/kit/<int:id_>/foto/<int:foto_id>/principal", methods=["POST"])
    @auth.exige_permissao("catalog.edit")
    def definir_capa_kit(id_, foto_id):
        dados.definir_foto_principal_kit(foto_id, id_)
        flash("Foto de capa definida.", "ok")
        return redirect(url_for("editar_kit", id_=id_))

    # ------------------------------------------------------------------
    # Catalogo publico
    # ------------------------------------------------------------------

    @app.route("/catalogo")
    def catalogo():
        busca = request.args.get("q", "")
        cat_id = request.args.get("categoria", "")
        cat_id_int = None
        if cat_id:
            try:
                cat_id_int = int(cat_id)
            except (TypeError, ValueError):
                cat_id = ""

        produtos = dados.catalogo_produtos(cat_id_int, busca or None)
        kits = dados.catalogo_kits(busca or None)
        org = dados.organizacao()

        return render_template("catalogo.html",
                               produtos=produtos,
                               kits=kits,
                               categorias=dados.listar_categorias(),
                               busca=busca,
                               cat_filtro=cat_id,
                               org=org)

    @app.route("/catalogo/produto/<int:id_>")
    def catalogo_produto(id_):
        produto = dados.produto_publico(id_)
        if not produto:
            abort(404)
        org = dados.organizacao()
        return render_template("catalogo_produto.html",
                               produto=produto, org=org)

    @app.route("/catalogo/kit/<int:id_>")
    def catalogo_kit(id_):
        kit = dados.kit_publico(id_)
        if not kit:
            abort(404)
        org = dados.organizacao()
        return render_template("catalogo_kit.html",
                               kit=kit, org=org)

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

    DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]

    @app.route("/agenda")
    @auth.exige_permissao("agenda.view")
    def agenda():
        import calendar as cal_mod
        from datetime import date, timedelta

        hoje = date.fromisoformat(formato.agora()[:10])
        visao = request.args.get("visao", "mensal")
        tipo = request.args.get("tipo") or None
        status = request.args.get("status") or None

        ano = request.args.get("ano", type=int) or hoje.year
        mes = request.args.get("mes", type=int) or hoje.month
        dia = request.args.get("dia", type=int) or hoje.day

        if mes < 1 or mes > 12:
            mes = hoje.month

        if visao == "semanal":
            ref = date(ano, mes, min(dia, cal_mod.monthrange(ano, mes)[1]))
            seg = ref - timedelta(days=ref.weekday())
            dom = seg + timedelta(days=6)
            data_inicio = seg.isoformat()
            data_fim = dom.isoformat()
            eventos = dados.eventos_agenda(data_inicio, data_fim, tipo, status)
            hist = dados.eventos_historico(data_inicio, data_fim)
            dias_semana = []
            for i in range(7):
                d = seg + timedelta(days=i)
                evts_dia = _eventos_do_dia(eventos, d.isoformat(), hist)
                dias_semana.append({"data": d, "eventos": evts_dia})
            sem_ant = seg - timedelta(days=7)
            sem_prox = seg + timedelta(days=7)
            return render_template("agenda.html",
                                   visao=visao, tipo=tipo or "",
                                   status=status or "",
                                   dias_semana=dias_semana,
                                   seg=seg, dom=dom,
                                   sem_ant=sem_ant, sem_prox=sem_prox,
                                   meses=MESES_PT, hoje=hoje,
                                   status_comercial=dados.STATUS_PEDIDO_COMERCIAL)

        if visao == "diaria":
            try:
                ref = date(ano, mes, dia)
            except ValueError:
                ref = hoje
            data_str = ref.isoformat()
            eventos = dados.eventos_agenda(data_str, data_str, tipo, status)
            hist = dados.eventos_historico(data_str, data_str)
            retiradas = [e for e in eventos if e.get("data_retirada") == data_str]
            devolucoes = [e for e in eventos if e.get("data_devolucao") == data_str]
            eventos_dia = [e for e in eventos if e.get("data_evento") == data_str]
            historicos = [h for h in hist if h.get("data_evento") == data_str]
            preparacoes = [e for e in eventos
                           if e.get("status_operacional") == "preparacao"
                           and e.get("data_retirada")
                           and e["data_retirada"] >= data_str]
            ant = ref - timedelta(days=1)
            prox = ref + timedelta(days=1)
            return render_template("agenda.html",
                                   visao=visao, tipo=tipo or "",
                                   status=status or "",
                                   ref=ref,
                                   retiradas=retiradas,
                                   devolucoes=devolucoes,
                                   eventos_dia=eventos_dia,
                                   historicos=historicos,
                                   preparacoes=preparacoes,
                                   ant=ant, prox=prox,
                                   meses=MESES_PT, hoje=hoje,
                                   status_comercial=dados.STATUS_PEDIDO_COMERCIAL)

        # mensal (default)
        _, ultimo_dia = cal_mod.monthrange(ano, mes)
        data_inicio = f"{ano}-{mes:02d}-01"
        data_fim = f"{ano}-{mes:02d}-{ultimo_dia:02d}"
        eventos = dados.eventos_agenda(data_inicio, data_fim, tipo, status)
        hist = dados.eventos_historico(data_inicio, data_fim)

        primeiro_dia_semana = cal_mod.weekday(ano, mes, 1)
        offset_dia = (primeiro_dia_semana + 1) % 7

        calendario = []
        for d in range(1, ultimo_dia + 1):
            data_str = f"{ano}-{mes:02d}-{d:02d}"
            evts = _eventos_do_dia(eventos, data_str, hist)
            calendario.append({"dia": d, "data": data_str, "eventos": evts})

        if mes == 1:
            ano_ant, mes_ant = ano - 1, 12
        else:
            ano_ant, mes_ant = ano, mes - 1
        if mes == 12:
            ano_prox, mes_prox = ano + 1, 1
        else:
            ano_prox, mes_prox = ano, mes + 1

        return render_template("agenda.html",
                               visao=visao, tipo=tipo or "",
                               status=status or "",
                               calendario=calendario,
                               offset_dia=offset_dia,
                               ano=ano, mes=mes,
                               ano_ant=ano_ant, mes_ant=mes_ant,
                               ano_prox=ano_prox, mes_prox=mes_prox,
                               meses=MESES_PT, hoje=hoje,
                               status_comercial=dados.STATUS_PEDIDO_COMERCIAL)

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
