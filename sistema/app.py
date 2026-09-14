"""Aplicacao Flask — Morumbi Festas."""

import io
import os

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from sistema import auth, dados, formato, listas

UPLOAD_EXTENSOES = {".jpg", ".jpeg", ".png", ".webp"}
UPLOAD_MAX_MB = 10


MENU = (
    ("Painel", (
        ("painel", "Dashboard"),
    )),
    ("Comercial", (
        ("lista_clientes", "Clientes"),
        ("lista_leads", "Leads"),
        ("lista_orcamentos", "Orcamentos"),
        ("lista_pedidos", "Pedidos"),
    )),
    ("Catalogo", (
        ("catalogo_interno", "Vitrine"),
        ("lista_produtos", "Produtos"),
        ("lista_kits", "Kits"),
        ("lista_categorias", "Categorias"),
    )),
    ("Administracao", (
        ("lista_usuarios", "Usuarios"),
        ("lista_origens", "Origens de lead"),
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

    # ------------------------------------------------------------------
    # Clientes
    # ------------------------------------------------------------------

    @app.route("/clientes")
    @auth.exige_perfil("admin", "comercial")
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

        busca = request.args.get("q", "")
        lista = listas.filtrar(lista, busca, listas.BUSCA_CLIENTES)

        ordem = request.args.get("ordem", "")
        invertido = request.args.get("dir") == "desc"
        lista = listas.ordenar(lista, ordem, listas.ORDENS_CLIENTES, invertido)

        todas_tags = _todas_tags_clientes()

        return render_template("clientes.html", clientes=lista,
                               ver=ver, busca=busca, ordem=ordem,
                               invertido=invertido,
                               ordens=listas.ORDENS_CLIENTES,
                               origens=dados.ORIGENS_CLIENTE,
                               origem_filtro=origem,
                               tag_filtro=tag,
                               todas_tags=todas_tags)

    def _todas_tags_clientes() -> list:
        with dados.conectar() as conn:
            return [r["tag"] for r in conn.execute(
                "SELECT DISTINCT tag FROM tags_cliente ORDER BY tag"
            ).fetchall()]

    @app.route("/cliente", methods=["GET", "POST"])
    @app.route("/cliente/<int:id_>", methods=["GET", "POST"])
    @auth.exige_perfil("admin", "comercial")
    def editar_cliente(id_=None):
        cliente = dados.buscar_cliente(id_) if id_ else None
        if id_ and not cliente:
            abort(404)

        if request.method == "POST":
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

        return render_template("cliente.html",
                               atual=cliente or {},
                               origens=dados.ORIGENS_CLIENTE)

    @app.route("/clientes/exportar")
    @auth.exige_perfil("admin", "comercial")
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
    @auth.exige_login
    def lista_categorias():
        arvore = dados.categorias_arvore()
        return render_template("categorias.html", arvore=arvore,
                               categorias=dados.listar_categorias())

    @app.route("/categoria", methods=["POST"])
    @auth.exige_perfil("admin")
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
    @auth.exige_perfil("admin")
    def excluir_categoria(id_):
        try:
            dados.excluir_categoria(id_)
            dados.registrar_acao(
                session.get("usuario_id"), "categoria_excluiu",
                f"Excluiu categoria {id_}", {"categoria_id": id_})
            flash("Categoria excluida.", "ok")
        except dados.ErroDeCampo as e:
            flash(str(e), "erro")
        return redirect(url_for("lista_categorias"))

    # ------------------------------------------------------------------
    # Produtos
    # ------------------------------------------------------------------

    @app.route("/produtos")
    @auth.exige_login
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
        with dados.conectar() as conn:
            return [r["tag"] for r in conn.execute(
                "SELECT DISTINCT tag FROM tags_produto ORDER BY tag"
            ).fetchall()]

    @app.route("/produto", methods=["GET", "POST"])
    @app.route("/produto/<int:id_>", methods=["GET", "POST"])
    @auth.exige_perfil("admin")
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
    @auth.exige_perfil("admin")
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
            flash("Formato invalido. Use JPG, PNG ou WebP.", "erro")
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
    @auth.exige_perfil("admin")
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
            flash("Foto excluida.", "ok")
        return redirect(url_for("editar_produto", id_=id_))

    @app.route("/produto/<int:id_>/foto/<int:foto_id>/principal", methods=["POST"])
    @auth.exige_perfil("admin")
    def definir_capa(id_, foto_id):
        dados.definir_foto_principal(foto_id, id_)
        flash("Foto de capa definida.", "ok")
        return redirect(url_for("editar_produto", id_=id_))

    # ------------------------------------------------------------------
    # Kits
    # ------------------------------------------------------------------

    @app.route("/kits")
    @auth.exige_login
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
    @auth.exige_perfil("admin")
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
    @auth.exige_perfil("admin")
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
            flash("Produto e quantidade invalidos.", "erro")
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
    @auth.exige_perfil("admin")
    def remover_item_kit(id_, item_id):
        dados.remover_item_kit(item_id)
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
    @auth.exige_perfil("admin")
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
            flash("Formato invalido. Use JPG, PNG ou WebP.", "erro")
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
    @auth.exige_perfil("admin")
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
            flash("Foto excluida.", "ok")
        return redirect(url_for("editar_kit", id_=id_))

    @app.route("/kit/<int:id_>/foto/<int:foto_id>/principal", methods=["POST"])
    @auth.exige_perfil("admin")
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
    @auth.exige_login
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
    @auth.exige_perfil("admin")
    def lista_origens():
        return render_template("origens.html",
                               origens=dados.listar_origens())

    @app.route("/origem", methods=["POST"])
    @auth.exige_perfil("admin")
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
    @auth.exige_perfil("admin")
    def excluir_origem_rota(id_):
        try:
            dados.excluir_origem(id_)
            flash("Origem excluida.", "ok")
        except ValueError as e:
            flash(str(e), "erro")
        return redirect(url_for("lista_origens"))

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------

    @app.route("/leads")
    @auth.exige_perfil("admin", "comercial")
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
    @auth.exige_perfil("admin", "comercial")
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
    @auth.exige_perfil("admin", "comercial")
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
    @auth.exige_perfil("admin", "comercial")
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
    @auth.exige_perfil("admin", "comercial")
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
                    f"{'Editou' if id_ else 'Criou'} orcamento #{novo_id}")
                flash("Orcamento salvo.", "ok")
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
    @auth.exige_perfil("admin", "comercial")
    def converter_orcamento(id_):
        try:
            pedido_id = dados.converter_orcamento_em_pedido(id_)
            dados.registrar_acao(
                session.get("usuario_id"), "pedido",
                f"Converteu orcamento #{id_} em pedido #{pedido_id}")
            flash(f"Pedido #{pedido_id} criado a partir do orcamento.", "ok")
            return redirect(url_for("ver_pedido", id_=pedido_id))
        except ValueError as e:
            flash(str(e), "erro")
            return redirect(url_for("editar_orcamento", id_=id_))

    # ------------------------------------------------------------------
    # Pedidos
    # ------------------------------------------------------------------

    @app.route("/pedidos")
    @auth.exige_perfil("admin", "comercial")
    def lista_pedidos():
        busca = request.args.get("q", "")
        peds = dados.listar_pedidos()
        if busca:
            from sistema.listas import filtrar, BUSCA_PEDIDOS
            peds = filtrar(peds, busca, BUSCA_PEDIDOS)
        return render_template("pedidos.html", pedidos=peds, busca=busca)

    @app.route("/pedido/<int:id_>")
    @auth.exige_perfil("admin", "comercial")
    def ver_pedido(id_):
        ped = dados.buscar_pedido_festas(id_)
        if not ped:
            abort(404)
        return render_template("pedido_festas.html", pedido=ped)

    return app
