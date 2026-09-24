"""Sprint 2.2 — empresas (tenants): isolamento, permissões e migração."""

import json
import re

import pytest
from werkzeug.security import generate_password_hash

from sistema import dados, formato, permissoes

SENHA = "senha123"


def _usuario(login, perfil="admin", tenant=None):
    with dados.usando_tenant(tenant or dados.tenant_padrao()):
        return dados.salvar_usuario({"nome": login.title(), "login": login,
                                     "perfil": perfil},
                                    senha_hash=generate_password_hash(SENHA))


def _entrar(app, login):
    c = app.test_client()
    r = c.post("/entrar", data={"login": login, "senha": SENHA})
    assert r.status_code == 302, r.text[:300]
    return c


def _semear(marca: str) -> dict:
    """Um conjunto completo de dados da empresa atual, com nomes marcados."""
    agora = formato.agora()
    cli = dados.salvar_cliente({"nome": f"Cliente {marca}", "whatsapp": "67999990000",
                                "email": "mesmo@exemplo.com"})
    cat = dados.salvar_categoria(f"Categoria {marca}")
    prod = dados.salvar_produto({"nome": f"Produto {marca}", "categoria_id": cat,
                                 "quantidade_total": 5, "publicado": 1})
    kit = dados.salvar_kit({"nome": "Kit Safari", "descricao": f"Kit {marca}",
                            "publicado": 1})
    dados.adicionar_item_kit(kit, prod, 1)
    origem = dados.salvar_origem(f"Origem {marca}")
    lead = dados.salvar_lead({"cliente_id": cli, "origem_id": origem,
                              "interesse": f"Lead {marca}"})
    orc = dados.salvar_orcamento({"cliente_id": cli, "lead_id": lead,
                                  "observacoes": f"Orçamento {marca}"},
                                 [{"tipo": "produto", "item_id": prod,
                                   "descricao": f"Produto {marca}", "quantidade": 1,
                                   "preco_unitario": 100}])
    ped = dados.salvar_pedido_festas(
        {"cliente_id": cli, "data_evento": "2026-11-10", "data_retirada": "2026-11-09",
         "data_devolucao": "2026-11-11", "observacoes": f"Pedido {marca}"},
        [{"tipo": "produto", "item_id": prod, "descricao": f"Produto {marca}",
          "quantidade": 2, "preco_unitario": 150}])
    fin = dados.salvar_pedido_festas(
        {"cliente_id": cli, "data_evento": "2026-10-01", "status_comercial": "finalizado",
         "status_operacional": "finalizado", "observacoes": f"Faturado {marca}"},
        [{"tipo": "kit", "item_id": kit, "descricao": "Kit Safari", "quantidade": 1,
          "preco_unitario": 777 if marca == "Alfa" else 555}])
    hist = dados.salvar_evento_historico({
        "cliente_id": cli, "origem_id": 594, "origem": "Formulario Festas",
        "data_evento": "2026-12-05", "descricao": f"Importado {marca}",
        "status_origem": "aprovado"})
    with dados.conectar() as conn:
        foto = conn.execute("INSERT INTO fotos_produto (produto_id, arquivo, principal,"
                            " criado_em) VALUES (?, 'x.png', 1, ?)", (prod, agora)).lastrowid
        item_kit = conn.execute("SELECT id FROM itens_kit WHERE kit_id = ?",
                                (kit,)).fetchone()[0]
    return {"cliente": cli, "categoria": cat, "produto": prod, "kit": kit,
            "origem": origem, "lead": lead, "orcamento": orc, "pedido": ped,
            "finalizado": fin, "historico": hist, "foto": foto, "item_kit": item_kit}


@pytest.fixture()
def duas(app):
    """Empresa A (Morumbi Festas, a principal) e Empresa B (teste)."""
    a = dados.tenant_padrao()
    b = dados.criar_empresa("Empresa Teste")
    with dados.usando_tenant(a):
        ids_a = _semear("Alfa")
    with dados.usando_tenant(b):
        ids_b = _semear("Beta")
    ids_a["usuario"] = _usuario("usu_a", tenant=a)
    ids_b["usuario"] = _usuario("usu_b", tenant=b)
    return {"a": a, "b": b, "ids_a": ids_a, "ids_b": ids_b}


def _instantaneo():
    """Tudo o que existe no banco, para provar que nada mudou."""
    with dados.conectar() as conn:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
                for t in ("clientes", "categorias", "produtos", "fotos_produto", "kits",
                          "itens_kit", "origens_lead", "leads", "orcamentos",
                          "itens_orcamento", "pedidos", "itens_pedido",
                          "eventos_historico", "configuracoes", "servicos", "membros")}


# Rotas com id: qual registro da outra empresa usar em cada uma.
_ID_DA_ROTA = {
    "catalogo_kit": "kit", "catalogo_produto": "produto", "editar_cliente": "cliente",
    "disponibilidade_produto": "produto", "editar_kit": "kit", "editar_lead": "lead",
    "ver_pedido_operacional": "pedido", "editar_orcamento": "orcamento",
    "ver_pedido": "pedido", "editar_pedido": "pedido",
    "ver_pedido_historico": "historico", "editar_produto": "produto",
    "editar_usuario": "usuario",
}
_MARCAS = ("Alfa", "Beta")


def _get_de_todas_as_rotas(app, cliente, ids_outra):
    """Visita cada rota GET; nas que recebem id, usa o registro da outra empresa."""
    respostas = {}
    for regra in app.url_map.iter_rules():
        if "GET" not in regra.methods or regra.endpoint in ("static", "sair"):
            continue
        args = {}
        for arg in regra.arguments:
            chave = _ID_DA_ROTA.get(regra.endpoint)
            assert chave, f"rota sem mapeamento de id: {regra.rule}"
            args[arg] = ids_outra[chave]
        with app.test_request_context():
            from flask import url_for
            url = url_for(regra.endpoint, **args)
        respostas[url] = cliente.get(url)
    return respostas


class TesteIsolamento:
    def test_cada_usuario_ve_so_a_propria_empresa_em_todas_as_rotas(self, app, duas):
        for login, propria, outra in (("usu_a", "Alfa", "Beta"), ("usu_b", "Beta", "Alfa")):
            c = _entrar(app, login)
            ids_outra = duas["ids_b" if outra == "Beta" else "ids_a"]
            for url, r in _get_de_todas_as_rotas(app, c, ids_outra).items():
                texto = r.get_data(as_text=True)
                assert f"Cliente {outra}" not in texto, (login, url)
                assert f"Produto {outra}" not in texto, (login, url)
                assert f"Pedido {outra}" not in texto, (login, url)
                assert f"Importado {outra}" not in texto, (login, url)
                if re.search(r"/\d+", url) and not url.startswith("/api"):
                    assert r.status_code in (404, 302, 403), (login, url, r.status_code)

    def test_usuario_a_acessa_a_e_b_acessa_b(self, app, duas):
        for login, marca, ids in (("usu_a", "Alfa", duas["ids_a"]),
                                  ("usu_b", "Beta", duas["ids_b"])):
            c = _entrar(app, login)
            assert f"Cliente {marca}" in c.get("/clientes").text
            assert f"Pedido {marca}" in c.get(f"/pedido/{ids['pedido']}").text
            assert f"Produto {marca}" in c.get("/produtos").text
            assert c.get(f"/pedido/{ids['pedido']}").status_code == 200

    def test_nenhuma_acao_altera_dados_da_outra_empresa(self, app, duas):
        c = _entrar(app, "usu_b")
        a = duas["ids_a"]
        antes = _instantaneo()
        tentativas = [
            (f"/cliente/{a['cliente']}", {"nome": "Invadido"}),
            (f"/produto/{a['produto']}", {"nome": "Invadido"}),
            (f"/kit/{a['kit']}", {"nome": "Invadido"}),
            (f"/kit/{a['kit']}/item", {"produto_id": a["produto"], "quantidade": 3}),
            (f"/kit/{a['kit']}/item/{a['item_kit']}/remover", {}),
            (f"/produto/{a['produto']}/foto/{a['foto']}/excluir", {}),
            (f"/produto/{a['produto']}/foto/{a['foto']}/principal", {}),
            (f"/categoria/{a['categoria']}/excluir", {}),
            ("/categoria", {"id": a["categoria"], "nome": "Invadida"}),
            (f"/origem/{a['origem']}/excluir", {}),
            ("/origem", {"id": a["origem"], "nome": "Invadida"}),
            (f"/lead/{a['lead']}", {"cliente_id": a["cliente"], "status": "perdido"}),
            (f"/lead/{a['lead']}/mover", {"status": "perdido"}),
            (f"/orcamento/{a['orcamento']}", {"cliente_id": a["cliente"]}),
            (f"/orcamento/{a['orcamento']}/converter", {}),
            (f"/pedido/{a['pedido']}/editar", {"cliente_id": a["cliente"]}),
            (f"/pedido/{a['pedido']}/cancelar", {"motivo": "x"}),
            (f"/pedido/{a['pedido']}/finalizar", {}),
            (f"/pedido/{a['pedido']}/ocorrencia", {"texto": "x"}),
            (f"/operacao/pedido/{a['pedido']}/avancar", {}),
            (f"/pedido/historico/{a['historico']}/converter", {}),
            (f"/faturamento/historico/{a['historico']}", {"valor": "999"}),
        ]
        for url, form in tentativas:
            c.post(url, data=form)
        assert _instantaneo() == antes

    def test_referencias_da_outra_empresa_sao_recusadas(self, app, duas):
        a = duas["ids_a"]
        with dados.usando_tenant(duas["b"]):
            with pytest.raises(dados.ErroDeCampo):
                dados.salvar_pedido_festas({"cliente_id": a["cliente"]}, [])
            b_cli = duas["ids_b"]["cliente"]
            with pytest.raises(dados.ErroDeCampo):
                dados.salvar_pedido_festas(
                    {"cliente_id": b_cli},
                    [{"tipo": "produto", "item_id": a["produto"], "descricao": "x",
                      "quantidade": 1, "preco_unitario": 1}])
            with pytest.raises(dados.ErroDeCampo):
                dados.salvar_lead({"cliente_id": b_cli, "origem_id": a["origem"]})
            with pytest.raises(dados.ErroDeCampo):
                dados.salvar_produto({"nome": "X", "categoria_id": a["categoria"]})

    def test_mesmo_nome_de_kit_sao_registros_diferentes(self, app, duas):
        assert duas["ids_a"]["kit"] != duas["ids_b"]["kit"]
        with dados.usando_tenant(duas["b"]):
            assert [k["id"] for k in dados.listar_kits()] == [duas["ids_b"]["kit"]]
            # cliente com o mesmo WhatsApp/e-mail de outra empresa não é duplicidade
            assert dados.verificar_duplicidade("whatsapp", "67999990000",
                                               duas["ids_b"]["cliente"]) is None

    def test_bi_agenda_esteira_e_estoque_por_empresa(self, app, duas):
        with dados.usando_tenant(duas["a"]):
            fat_a = dados.faturamento_periodo("2026-01-01", "2026-12-31")
            agenda_a = dados.eventos_agenda("2026-11-01", "2026-11-30")
            esteira_a = sum(e["total"] for e in dados.esteira_pedidos())
            disp_a = dados.disponibilidade(duas["ids_a"]["produto"], "2026-11-09",
                                           "2026-11-11")
            assert dados.disponibilidade(duas["ids_b"]["produto"]) == 0
        with dados.usando_tenant(duas["b"]):
            fat_b = dados.faturamento_periodo("2026-01-01", "2026-12-31")
        assert (fat_a["total"], fat_b["total"]) == (777, 555)
        assert [e["id"] for e in agenda_a] == [duas["ids_a"]["pedido"]]
        assert esteira_a == 1
        assert disp_a == 3  # 5 no estoque de A, 2 reservados pelo pedido de A

    def test_busca_e_api_ficam_na_empresa(self, app, duas):
        c = _entrar(app, "usu_b")
        assert "Cliente Alfa" not in c.get("/pedidos?q=Alfa").text
        assert "Cliente Alfa" not in c.get("/pedidos?q=594&parcial=1").text
        assert "Cliente Beta" in c.get("/pedidos?q=594&parcial=1").text
        assert c.get(f"/api/disponibilidade?produto_id={duas['ids_a']['produto']}"
                     ).get_json() == {"disponivel": 0}
        corpo = json.dumps(c.get("/api/painel").get_json())
        assert "Alfa" not in corpo
        assert c.get("/api/faturamento?periodo=este_ano").get_json()["total"] in (0, 555)

    def test_catalogo_publico_mostra_so_a_empresa_principal(self, app, duas):
        anonimo = app.test_client()
        texto = anonimo.get("/catalogo").text
        assert "Produto Alfa" in texto and "Produto Beta" not in texto
        assert anonimo.get(f"/catalogo/produto/{duas['ids_b']['produto']}").status_code == 404

    def test_empresa_da_sessao_nao_vem_da_tela(self, app, duas):
        c = _entrar(app, "usu_b")
        # tentar trocar a empresa por parâmetro ou formulário não tem efeito
        assert "Cliente Alfa" not in c.get(f"/clientes?tenant_id={duas['a']}").text
        c.post("/cliente", data={"nome": "Nova B", "tenant_id": duas["a"]})
        with dados.conectar() as conn:
            assert conn.execute("SELECT tenant_id FROM clientes WHERE nome = 'Nova B'"
                                ).fetchone()[0] == duas["b"]
        # sessão adulterada para outra empresa sem vínculo: acesso encerrado
        with c.session_transaction() as s:
            s["tenant_id"] = duas["a"]
        r = c.get("/clientes")
        assert r.status_code == 302 and "/entrar" in r.headers["Location"]

    def test_empresa_suspensa_ou_inativa_nao_entra(self, app, duas):
        c = _entrar(app, "usu_b")
        with dados.conectar() as conn:
            conn.execute("UPDATE organizacoes SET status = 'suspenso' WHERE id = ?",
                         (duas["b"],))
        assert c.get("/pedidos").status_code == 302
        r = app.test_client().post("/entrar", data={"login": "usu_b", "senha": SENHA})
        assert r.status_code == 200 and "suspensa" in r.text
        with dados.conectar() as conn:
            conn.execute("UPDATE organizacoes SET status = 'inativo' WHERE id = ?",
                         (duas["b"],))
        assert app.test_client().post("/entrar", data={"login": "usu_b", "senha": SENHA}
                                      ).status_code == 200
        _entrar(app, "usu_a")  # a outra empresa segue normal

    def test_usuarios_e_configuracoes_sao_da_empresa(self, app, duas):
        c = _entrar(app, "usu_b")
        texto = c.get("/usuarios").text
        assert "Usu_B" in texto and "Usu_A" not in texto
        uid_a = dados.buscar_usuario_por_login("usu_a")["id"]
        assert c.get(f"/usuario/{uid_a}").status_code == 404
        c.post("/configuracoes/sistema", data={
            "idioma": "pt-BR", "moeda": "BRL", "fuso_horario": "America/Sao_Paulo",
            "formato_data": "DD/MM/YYYY", "cor_primaria": "#112233",
            "cor_secundaria": "#445566"})
        with dados.usando_tenant(duas["b"]):
            assert dados.config("fuso_horario") == "America/Sao_Paulo"
        with dados.usando_tenant(duas["a"]):
            assert dados.config("fuso_horario") == "America/Campo_Grande"
            assert dados.config("cor_primaria") == "#6F1C85"
        with dados.usando_tenant(duas["b"]):
            assert [s["nome"] for s in dados.listar_servicos()] == list(dados.SERVICOS_PADRAO)

    def test_auditoria_registra_empresa_usuario_e_valores(self, app, duas):
        c = _entrar(app, "usu_b")
        pid = duas["ids_b"]["pedido"]
        c.post(f"/operacao/pedido/{pid}/avancar", data={})
        uid = dados.buscar_usuario_por_login("usu_b")["id"]
        with dados.conectar() as conn:
            r = conn.execute("SELECT * FROM audit_log WHERE entidade = 'pedido'"
                             " AND entidade_id = ? ORDER BY id DESC", (pid,)).fetchone()
        d = json.loads(r["dados"])
        assert (r["tenant_id"], r["usuario_id"]) == (duas["b"], uid)
        assert d["mudancas"]["status_operacional"] == ["preparacao", "separado"]
        with dados.usando_tenant(duas["a"]):
            assert all(a["tenant_id"] == duas["a"] for a in dados.listar_audit(500))


# --- Permissões -------------------------------------------------------------

PERFIS_TESTADOS = ("admin", "gestor", "comercial", "operacional", "financeiro",
                   "visualizacao")
# (método, rota, permissão que libera)
ACESSOS = (
    ("GET", "/usuarios", "users.view"),
    ("GET", "/configuracoes/empresa", "settings.view"),
    ("GET", "/configuracoes/permissoes", "users.view"),
    ("GET", "/clientes", "customers.view"),
    ("GET", "/pedidos", "orders.view"),
    ("GET", "/faturamento", "reports.view"),
    ("GET", "/operacao", "operation.view"),
    ("GET", "/leads", "leads.view"),
    ("GET", "/agenda", "agenda.view"),
    ("GET", "/produtos", "catalog.view"),
    ("GET", "/pedido/novo", "orders.create"),
)


class TestePermissoes:
    @pytest.fixture()
    def logados(self, app):
        return {p: (_usuario(f"u_{p}", p), _entrar(app, f"u_{p}"))[1]
                for p in PERFIS_TESTADOS}

    def test_rotas_seguem_a_permissao_de_cada_perfil(self, app, logados):
        for perfil, c in logados.items():
            for metodo, url, perm in ACESSOS:
                r = c.open(url, method=metodo)
                pode = perm in permissoes.do_perfil(perfil)
                assert (r.status_code == 200) == pode, (perfil, url, r.status_code)
                if not pode:
                    assert r.status_code == 403

    def test_sem_orders_edit_nao_altera_pedido(self, app, logados):
        cli = dados.salvar_cliente({"nome": "Cliente P"})
        pid = dados.salvar_pedido_festas({"cliente_id": cli, "data_evento": "2026-11-01"},
                                         [])
        antes = _instantaneo()
        for perfil in ("gestor", "operacional", "financeiro", "visualizacao"):
            assert "orders.edit" not in permissoes.do_perfil(perfil)
            r = logados[perfil].post(f"/pedido/{pid}/editar",
                                     data={"cliente_id": cli, "observacoes": "x"})
            assert r.status_code == 403
            assert logados[perfil].post(f"/pedido/{pid}/cancelar").status_code == 403
        assert _instantaneo() == antes

    def test_sem_users_manage_nao_gerencia_usuarios(self, app, logados):
        for perfil in ("gestor", "comercial", "operacional", "financeiro",
                       "visualizacao"):
            c = logados[perfil]
            assert c.post("/usuario", data={"nome": "X", "login": f"x_{perfil}",
                                            "senha": "1", "perfil": "admin"}
                          ).status_code == 403
            assert c.post("/configuracoes/empresa", data={"nome": "X"}).status_code == 403
        assert dados.buscar_usuario_por_login("x_comercial") is None
        assert dados.nome_empresa() == "Morumbi Festas"

    def test_administrador_tem_tudo_e_gerencia(self, app, logados):
        assert permissoes.do_perfil("admin") == permissoes.TODAS
        c = logados["admin"]
        r = c.post("/usuario", data={"nome": "Livia", "login": "livia", "senha": "1",
                                     "email": "livia@x.com", "perfil": "financeiro"})
        assert r.status_code == 302
        u = dados.buscar_usuario(dados.buscar_usuario_por_login("livia")["id"])
        assert (u["perfil"], u["email"], u["tenant_id"]) == (
            "financeiro", "livia@x.com", dados.tenant_padrao())
        assert c.post(f"/usuario/{u['id']}/alternar").status_code == 302
        assert dados.buscar_usuario(u["id"])["ativo"] == 0

    def test_empresa_nunca_fica_sem_administrador(self, app, logados):
        admins = [u for u in dados.listar_usuarios() if u["perfil"] == "admin"]
        for u in admins[1:]:
            dados.alternar_usuario(u["id"])
        unico = admins[0]
        with pytest.raises(dados.ErroDeCampo):
            dados.salvar_usuario(dict(unico, perfil="comercial"), id_=unico["id"])
        with pytest.raises(ValueError):
            dados.alternar_usuario(unico["id"])

    def test_menu_mostra_so_o_permitido(self, app, logados):
        texto = logados["visualizacao"].get("/").text
        assert "Pedidos" in texto and "Usuários" not in texto
        assert "Configurações" not in texto
        texto = logados["admin"].get("/").text
        assert "Empresa" in texto and "Permissões" in texto


# --- Configurações ------------------------------------------------------------

class TesteConfiguracoes:
    def test_empresa_salva_e_identidade_vem_do_cadastro(self, app, admin):
        r = admin.post("/configuracoes/empresa", data={
            "nome": "Morumbi Festas", "telefone": "6733330000", "whatsapp": "67999998888",
            "email": "contato@morumbi.com", "endereco": "Rua A, 1", "cidade": "Campo Grande",
            "instagram": "morumbifestas", "horario_funcionamento": "Seg a sáb, 8h às 18h"})
        assert r.status_code == 302
        e = dados.empresa_atual()
        assert (e["whatsapp"], e["email"]) == ("67999998888", "contato@morumbi.com")
        assert dados.config("horario_funcionamento") == "Seg a sáb, 8h às 18h"
        with dados.conectar() as conn:
            assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE entidade = 'empresa'"
                                ).fetchone()[0] == 1
        # o nome da empresa é a operação dos pedidos e o título das telas
        admin.post("/configuracoes/empresa", data={"nome": "Morumbi Festas",
                                                   "nome_fantasia": "Morumbi"})
        assert "<title>Pedidos — Morumbi</title>" in admin.get("/pedidos").text
        assert dados.origens_pedidos_unificados() == ["Morumbi"]

    def test_logo_da_empresa(self, app, admin, tmp_path):
        import io
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        r = admin.post("/configuracoes/empresa", data={
            "nome": "Morumbi Festas", "logo": (io.BytesIO(png), "logo.png")},
            content_type="multipart/form-data")
        assert r.status_code == 302
        logo = dados.empresa_atual()["logo"]
        import os
        arquivo = os.path.join(app.static_folder, logo)
        try:
            assert logo.startswith(f"uploads/empresas/{dados.tenant_padrao()}/logo-")
            assert os.path.exists(arquivo)
            assert logo in admin.get("/").text
        finally:
            if os.path.exists(arquivo):
                os.remove(arquivo)
        r = admin.post("/configuracoes/empresa", data={
            "nome": "Morumbi Festas", "logo": (io.BytesIO(b"x"), "logo.svg")},
            content_type="multipart/form-data")
        assert "Formato inválido" in r.text

    def test_operacao_servicos_e_sistema(self, app, admin):
        admin.post("/configuracoes/operacao", data={"prazo_preparacao_dias": "2",
                                                    "dias_orcamento_sem_retorno": "5",
                                                    "formas_pagamento": "Pix\nBoleto"})
        assert dados.parametro("dias_orcamento_sem_retorno") == "5"
        assert dados.lista_config("formas_pagamento") == ["Pix", "Boleto"]
        r = admin.post("/configuracoes/operacao", data={"prazo_preparacao_dias": "abc"})
        assert "número inteiro" in r.text
        admin.post("/configuracoes/servicos", data={"acao": "criar", "nome": "Decoração"})
        admin.post("/configuracoes/servicos", data={"acao": "criar", "nome": "decoracao"})
        nomes = [s["nome"] for s in dados.listar_servicos()]
        assert nomes.count("Decoração") == 1
        assert "Decoração" in admin.get("/pedido/novo").text
        r = admin.post("/configuracoes/sistema", data={
            "idioma": "pt-BR", "moeda": "BRL", "fuso_horario": "Europe/Paris",
            "formato_data": "DD/MM/YYYY", "cor_primaria": "#6F1C85",
            "cor_secundaria": "#FF8C00"})
        assert dados.config("fuso_horario") == "America/Campo_Grande"

    def test_padroes_da_morumbi(self, app):
        for chave, valor in (("moeda", "BRL"), ("idioma", "pt-BR"),
                             ("fuso_horario", "America/Campo_Grande"),
                             ("formato_data", "DD/MM/YYYY"),
                             ("cor_primaria", "#6F1C85"), ("cor_secundaria", "#FF8C00")):
            assert dados.config(chave) == valor
        e = dados.empresa_atual()
        assert (e["nome"], e["status"]) == ("Morumbi Festas", "ativo")
        assert e["plano_id"] is None and e["status_assinatura"] is None


# --- Migração -----------------------------------------------------------------

class TesteMigracao:
    def test_tudo_fica_na_empresa_principal_e_reinicializar_nao_muda_nada(self, app, duas):
        with dados.conectar() as conn:
            for t in dados.TABELAS_DA_EMPRESA:
                assert "tenant_id" in dados._colunas(conn, t)
            assert conn.execute("SELECT COUNT(*) FROM organizacoes").fetchone()[0] == 2
        antes = _instantaneo()
        dados.inicializar()
        dados.inicializar()
        assert _instantaneo() == antes
        with dados.conectar() as conn:
            assert conn.execute("SELECT COUNT(*) FROM organizacoes").fetchone()[0] == 2

    def test_usuario_antigo_vira_membro_uma_vez(self, app):
        with dados.conectar() as conn:
            conn.execute("INSERT INTO usuarios (nome, login, senha_hash, perfil, ativo)"
                         " VALUES ('Legado', 'legado', 'x', 'operacional', 0)")
        dados.inicializar()
        dados.inicializar()
        with dados.conectar() as conn:
            m = conn.execute("SELECT m.* FROM membros m JOIN usuarios u"
                             " ON u.id = m.usuario_id WHERE u.login = 'legado'").fetchall()
        assert len(m) == 1
        assert (m[0]["tenant_id"], m[0]["perfil"], m[0]["ativo"]) == (
            dados.tenant_padrao(), "operacional", 0)

    def test_parametro_antigo_vira_configuracao(self, app):
        with dados.conectar() as conn:
            conn.execute("INSERT INTO parametros (chave, valor)"
                         " VALUES ('dias_orcamento_sem_retorno', '9')")
        dados.inicializar()
        assert dados.parametro("dias_orcamento_sem_retorno") == "9"
