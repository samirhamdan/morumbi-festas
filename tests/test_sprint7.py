"""Sprint 7 — Catálogo, vitrine e disponibilidade.

Estoque real menos pedidos em andamento no período; kit limitado pela peça
mais escassa; histórico não bloqueia; pedir orçamento não reserva; o servidor
confere tudo de novo; concorrência; permissões; auditoria; endereços por slug.
"""

import json
import threading
from datetime import date, timedelta

import pytest
from werkzeug.security import generate_password_hash

from sistema import dados

HOJE = date.today()
FESTA = (HOJE + timedelta(days=30)).isoformat()
VESPERA = (HOJE + timedelta(days=29)).isoformat()


def _dia(n):
    return (HOJE + timedelta(days=n)).isoformat()


def _produto(nome="Mesa Safari", qtd=5, preco=50, publicado=1, **extra):
    return dados.salvar_produto(dict({"nome": nome, "quantidade_total": qtd,
                                      "preco_locacao": preco, "publicado": publicado,
                                      "status": "disponivel"}, **extra))


def _kit(nome="Kit Safari Encantado", preco=250, componentes=(), publicado=1, **extra):
    kid = dados.salvar_kit(dict({"nome": nome, "preco": preco, "publicado": publicado,
                                 "status": "ativo"}, **extra))
    for pid, qtd in componentes:
        dados.adicionar_item_kit(kid, pid, qtd)
    return kid


def _cliente(nome="Cliente Teste", whatsapp=""):
    return dados.salvar_cliente({"nome": nome, "whatsapp": whatsapp})


def _pedido(itens, retirada=VESPERA, evento=FESTA, devolucao=FESTA, **extra):
    return dados.salvar_pedido_festas(
        dict({"cliente_id": _cliente(f"Cli {len(dados.listar_clientes())}"),
              "data_evento": evento, "data_retirada": retirada, "data_devolucao": devolucao,
              "status_comercial": "confirmado", "status_operacional": "preparacao"}, **extra),
        [dict({"descricao": "Item", "preco_unitario": 10}, **i) for i in itens])


def _situacao(tipo, id_, data=FESTA, qtd=1):
    return dados.situacao_na_data(tipo, id_, data, qtd)


def _usuario(login, perfil):
    dados.salvar_usuario({"nome": login.title(), "login": login, "perfil": perfil},
                         senha_hash=generate_password_hash("123"))


def _entrar(app, login):
    c = app.test_client()
    c.post("/entrar", data={"login": login, "senha": "123"})
    return c


# ---------------------------------------------------------------------------
# Motor de disponibilidade
# ---------------------------------------------------------------------------

class TesteDisponibilidade:
    def test_estoque_menos_pedidos_ativos(self, app):
        p = _produto(qtd=5)
        assert _situacao("produto", p)["livres"] == 5
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 3}])
        s = _situacao("produto", p)
        assert s["livres"] == 2 and s["situacao"] == "disponivel"

    def test_ultimas_unidades_e_indisponivel(self, app):
        p = _produto(qtd=5)
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 4}])
        assert _situacao("produto", p)["situacao"] == "ultimas"
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 1}])
        s = _situacao("produto", p)
        assert s["situacao"] == "indisponivel" and s["livres"] == 0

    def test_quantidade_pedida_maior_que_livre(self, app):
        p = _produto(qtd=5)
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 3}])
        assert _situacao("produto", p, qtd=3)["situacao"] == "indisponivel"

    def test_kit_limitado_pela_peca_mais_escassa(self, app):
        a, b = _produto("Painel", qtd=10), _produto("Boleira", qtd=3)
        k = _kit(componentes=[(a, 2), (b, 1)])
        assert _situacao("kit", k)["capacidade"] == 3
        _pedido([{"tipo": "produto", "item_id": b, "quantidade": 2}])
        assert _situacao("kit", k)["livres"] == 1

    def test_kit_no_pedido_consome_as_pecas(self, app):
        a = _produto("Painel", qtd=4)
        k = _kit(componentes=[(a, 2)])
        _pedido([{"tipo": "kit", "item_id": k, "quantidade": 1}])
        assert _situacao("produto", a)["livres"] == 2

    def test_kit_nao_tem_estoque_proprio(self, app):
        cols = [r[1] for r in dados.conectar().execute("PRAGMA table_info(kits)")]
        assert "quantidade_total" not in cols

    def test_historico_nao_bloqueia(self, app):
        p = _produto(qtd=2)
        pid = _pedido([{"tipo": "produto", "item_id": p, "quantidade": 2}])
        assert _situacao("produto", p)["situacao"] == "indisponivel"
        with dados.conectar() as conn:
            conn.execute("UPDATE pedidos SET historico = 1 WHERE id = ?", (pid,))
        assert _situacao("produto", p)["livres"] == 2

    @pytest.mark.parametrize("status", ["cancelado", "finalizado"])
    def test_cancelado_e_finalizado_liberam(self, app, status):
        p = _produto(qtd=2)
        pid = _pedido([{"tipo": "produto", "item_id": p, "quantidade": 2}])
        with dados.conectar() as conn:
            conn.execute("UPDATE pedidos SET status_operacional = ? WHERE id = ?", (status, pid))
        assert _situacao("produto", p)["livres"] == 2

    def test_periodo_retirada_ate_devolucao(self, app):
        p = _produto(qtd=1)
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 1}],
                retirada=_dia(20), evento=_dia(21), devolucao=_dia(23))
        assert _situacao("produto", p, _dia(22))["situacao"] == "indisponivel"
        # festa em D com 2 dias de uso ocupa D-1..D: dia 25 fica livre
        assert _situacao("produto", p, _dia(25))["situacao"] != "indisponivel"

    def test_devolucao_e_saida_no_mesmo_dia(self, app):
        p = _produto(qtd=1)
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 1}],
                retirada=_dia(20), evento=_dia(21), devolucao=_dia(21))
        # festa no dia 22: a peça sairia no dia 21, quando ainda está voltando
        assert _situacao("produto", p, _dia(22))["situacao"] == "indisponivel"
        dados.salvar_produto({"nome": "Mesa Safari", "quantidade_total": 1, "preco_locacao": 50,
                              "publicado": 1, "status": "disponivel", "reuso_mesmo_dia": 1}, p)
        assert _situacao("produto", p, _dia(22))["situacao"] != "indisponivel"

    def test_pedido_so_com_data_do_evento_reserva(self, app):
        p = _produto(qtd=1)
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 1}],
                retirada="", evento=FESTA, devolucao="")
        assert _situacao("produto", p)["situacao"] == "indisponivel"

    def test_antecedencia_minima(self, app):
        p = _produto(qtd=5, dias_antecedencia=5)
        s = _situacao("produto", p, _dia(2))
        assert s["situacao"] == "indisponivel" and "antecedência" in s["motivo"]
        assert _situacao("produto", p, _dia(10))["situacao"] == "disponivel"

    def test_data_passada_indisponivel(self, app):
        p = _produto(qtd=5)
        assert _situacao("produto", p, _dia(-1))["situacao"] == "indisponivel"

    def test_manutencao_e_inativo_indisponiveis(self, app):
        p = _produto(qtd=5, status="manutencao")
        assert _situacao("produto", p)["situacao"] == "indisponivel"

    def test_kit_com_peca_inativa_fica_indisponivel(self, app):
        a = _produto("Painel", qtd=5)
        k = _kit(componentes=[(a, 1)])
        dados.definir_ativo("produto", a, False)
        assert _situacao("kit", k)["situacao"] == "indisponivel"


class TesteReservaEConcorrencia:
    def test_pedido_acima_do_estoque_e_recusado(self, app):
        p = _produto(qtd=2)
        with pytest.raises(dados.ErroDeCampo) as e:
            _pedido([{"tipo": "produto", "item_id": p, "quantidade": 3}])
        assert "Disponivel: 2" in str(e.value)

    def test_kit_acima_das_pecas_e_recusado(self, app):
        a = _produto("Painel", qtd=3)
        k = _kit(componentes=[(a, 2)])
        with pytest.raises(dados.ErroDeCampo):
            _pedido([{"tipo": "kit", "item_id": k, "quantidade": 2}])

    def test_kit_e_peca_avulsa_disputam_o_mesmo_estoque(self, app):
        a = _produto("Painel", qtd=3)
        k = _kit(componentes=[(a, 2)])
        with pytest.raises(dados.ErroDeCampo):
            _pedido([{"tipo": "kit", "item_id": k, "quantidade": 1},
                     {"tipo": "produto", "item_id": a, "quantidade": 2}])

    def test_ultima_unidade_com_dois_pedidos_ao_mesmo_tempo(self, app):
        p = _produto(qtd=1)
        clientes = [_cliente("Um"), _cliente("Dois")]
        tenant = dados.tenant_atual()
        largada = threading.Barrier(2)
        resultados = []

        def tentar(cid):
            with dados.usando_tenant(tenant):
                largada.wait()
                try:
                    dados.salvar_pedido_festas(
                        {"cliente_id": cid, "data_evento": FESTA, "data_retirada": VESPERA,
                         "data_devolucao": FESTA, "status_comercial": "confirmado",
                         "status_operacional": "preparacao"},
                        [{"tipo": "produto", "item_id": p, "descricao": "Mesa",
                          "quantidade": 1, "preco_unitario": 10}])
                    resultados.append("ok")
                except dados.ErroDeCampo:
                    resultados.append("recusado")

        linhas = [threading.Thread(target=tentar, args=(c,)) for c in clientes]
        for t in linhas:
            t.start()
        for t in linhas:
            t.join(20)
        assert sorted(resultados) == ["ok", "recusado"]
        assert _situacao("produto", p)["livres"] == 0

    def test_converter_orcamento_confere_estoque(self, app):
        p = _produto(qtd=1)
        orc = dados.salvar_orcamento(
            {"cliente_id": _cliente("Orc"), "data_evento": FESTA},
            [{"tipo": "produto", "item_id": p, "descricao": "Mesa", "quantidade": 1,
              "preco_unitario": 10}])
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 1}])
        with pytest.raises(ValueError):
            dados.converter_orcamento_em_pedido(orc)


# ---------------------------------------------------------------------------
# Orçamento pela vitrine
# ---------------------------------------------------------------------------

class TesteOrcamentoVitrine:
    def _contato(self, **extra):
        return dict({"nome": "Maria Festeira", "whatsapp": "(67) 99988-7766"}, **extra)

    def test_solicitar_cria_cliente_lead_e_orcamento_sem_reservar(self, app):
        p = _produto(qtd=2, preco=80)
        antes = _situacao("produto", p)["livres"]
        r = dados.solicitar_orcamento_catalogo(
            self._contato(), [{"tipo": "produto", "id": p, "quantidade": 2, "preco": 0.01}], FESTA)
        orc = dados.buscar_orcamento(r["orcamento_id"])
        assert orc["status"] == "rascunho" and orc["origem"] == "catalogo"
        assert orc["data_evento"] == FESTA
        assert orc["itens"][0]["preco_unitario"] == 80  # preço do servidor, nunca o do navegador
        assert orc["total"] == 160
        lead = dados.buscar_lead(r["lead_id"])
        assert lead["origem_nome"] == "Catálogo" and lead["status"] == "novo"
        assert _situacao("produto", p)["livres"] == antes  # nada reservado

    def test_nao_duplica_cliente_pelo_whatsapp(self, app):
        p = _produto(qtd=5)
        existente = _cliente("Maria", whatsapp="5567999887766")
        r = dados.solicitar_orcamento_catalogo(
            self._contato(), [{"tipo": "produto", "id": p, "quantidade": 1}], FESTA)
        assert r["cliente_id"] == existente and not r["cliente_novo"]
        r2 = dados.solicitar_orcamento_catalogo(
            self._contato(whatsapp="67 99988 7766"), [{"tipo": "produto", "id": p, "quantidade": 1}], FESTA)
        assert r2["cliente_id"] == existente
        assert len([c for c in dados.listar_clientes() if "Maria" in c["nome"]]) == 1

    def test_recusa_item_indisponivel_na_data(self, app):
        p = _produto(qtd=1)
        _pedido([{"tipo": "produto", "item_id": p, "quantidade": 1}])
        with pytest.raises(dados.ErroDeCampo) as e:
            dados.solicitar_orcamento_catalogo(
                self._contato(), [{"tipo": "produto", "id": p, "quantidade": 1}], FESTA)
        assert "Indisponível" in str(e.value)

    def test_recusa_conjunto_que_nao_cabe(self, app):
        a = _produto("Painel", qtd=3)
        k = _kit(componentes=[(a, 2)])
        with pytest.raises(dados.ErroDeCampo):
            dados.solicitar_orcamento_catalogo(
                self._contato(), [{"tipo": "kit", "id": k, "quantidade": 1},
                                  {"tipo": "produto", "id": a, "quantidade": 2}], FESTA)

    def test_ignora_item_nao_publicado_e_de_outra_empresa(self, app):
        oculto = _produto("Oculto", publicado=0)
        outra = dados.criar_empresa("Outra")
        with dados.usando_tenant(outra):
            alheio = _produto("Alheio")
        r = dados.resumo_orcamento_publico([{"tipo": "produto", "id": oculto},
                                            {"tipo": "produto", "id": alheio}])
        assert r["itens"] == []

    @pytest.mark.parametrize("contato,campo", [
        ({"nome": "", "whatsapp": "67999887766"}, "nome"),
        ({"nome": "Ana", "whatsapp": "123"}, "whatsapp"),
        ({"nome": "Ana", "whatsapp": "67999887766", "email": "x@"}, "email"),
    ])
    def test_valida_contato(self, app, contato, campo):
        p = _produto()
        with pytest.raises(dados.ErroDeCampo) as e:
            dados.solicitar_orcamento_catalogo(contato, [{"tipo": "produto", "id": p}], FESTA)
        assert e.value.campo == campo

    def test_data_passada_recusada(self, app):
        p = _produto()
        with pytest.raises(dados.ErroDeCampo) as e:
            dados.solicitar_orcamento_catalogo(self._contato(), [{"tipo": "produto", "id": p}], _dia(-2))
        assert e.value.campo == "data_evento"

    def test_grava_auditoria(self, app):
        p = _produto()
        r = dados.solicitar_orcamento_catalogo(self._contato(), [{"tipo": "produto", "id": p}], FESTA)
        registro = [a for a in dados.listar_audit(10) if a["tipo"] == "orcamento_catalogo"]
        assert registro and registro[0]["entidade_id"] == r["orcamento_id"]


class TesteRotasVitrine:
    def test_enderecos_por_slug(self, app, client):
        cat = dados.salvar_categoria("Infantil")
        a = _produto("Painel Redondo", categoria_id=cat)
        _kit("Kit Safari Encantado", componentes=[(a, 1)], categoria_id=cat)
        assert "Kit Safari Encantado" in client.get("/catalogo/kits").text
        assert "Painel Redondo" in client.get("/catalogo/pecas").text
        assert "Kit Safari Encantado" not in client.get("/catalogo/pecas").text
        r = client.get("/catalogo/infantil")
        assert r.status_code == 200 and "Painel Redondo" in r.text and "Kit Safari Encantado" in r.text
        r = client.get("/catalogo/kit-safari-encantado")
        assert r.status_code == 200 and "Adicionar ao orçamento" in r.text
        assert 'rel="canonical"' in r.text and "application/ld+json" in r.text

    def test_endereco_antigo_redireciona_301(self, app, client):
        p = _produto("Toalha Rosa")
        r = client.get(f"/catalogo/produto/{p}")
        assert r.status_code == 301 and r.headers["Location"].endswith("/catalogo/toalha-rosa")

    def test_nao_publicado_inativo_e_kit_vazio_dao_404(self, app, client):
        _produto("Escondido", publicado=0)
        p = _produto("Desligado")
        dados.definir_ativo("produto", p, False)
        _kit("Kit Vazio")
        for slug in ("escondido", "desligado", "kit-vazio", "nao-existe", "<script>"):
            assert client.get(f"/catalogo/{slug}").status_code == 404
        assert "Kit Vazio" not in client.get("/catalogo").text

    def test_api_disponibilidade_sem_numeros_de_estoque(self, app, client):
        p = _produto(qtd=7)
        r = client.get(f"/catalogo/api/disponibilidade?tipo=produto&id={p}&inicio={FESTA}&fim={_dia(36)}")
        corpo = r.get_json()
        assert r.status_code == 200 and len(corpo["dias"]) == 7
        assert set(corpo["dias"][0]) == {"data", "situacao", "rotulo", "motivo"}

    def test_api_disponibilidade_valida(self, app, client):
        p = _produto()
        oculto = _produto("Oculto", publicado=0)
        base = "/catalogo/api/disponibilidade?tipo=produto"
        assert client.get(f"{base}&id={p}&inicio=ontem").status_code == 400
        assert client.get(f"{base}&id={p}&inicio={FESTA}&fim={_dia(200)}").status_code == 400
        assert client.get(f"{base}&id={oculto}&inicio={FESTA}").status_code == 404

    def test_post_orcamento_exige_json(self, app, client):
        p = _produto()
        r = client.post("/catalogo/orcamento", data={"itens": "x"})
        assert r.status_code == 400
        r = client.post("/catalogo/orcamento", json={
            "contato": {"nome": "Ana", "whatsapp": "67999887766"}, "data": FESTA,
            "itens": [{"tipo": "produto", "id": p, "quantidade": 1}]})
        assert r.status_code == 200 and r.get_json()["ok"]
        assert dados.listar_orcamentos()[0]["origem"] == "catalogo"

    def test_armadilha_para_robos_nao_grava(self, app, client):
        p = _produto()
        r = client.post("/catalogo/orcamento", json={
            "contato": {"nome": "Bot", "whatsapp": "67999887766"}, "data": FESTA, "site": "spam",
            "itens": [{"tipo": "produto", "id": p, "quantidade": 1}]})
        assert r.get_json()["ok"] and dados.listar_orcamentos() == []

    def test_limite_de_envios_por_ip(self, app, client):
        p = _produto(qtd=50)
        corpo = {"contato": {"nome": "Ana", "whatsapp": "67999887766"}, "data": FESTA,
                 "itens": [{"tipo": "produto", "id": p, "quantidade": 1}]}
        codigos = [client.post("/catalogo/orcamento", json=corpo).status_code for _ in range(6)]
        assert codigos[:5] == [200] * 5 and codigos[5] == 429

    def test_resumo_recalcula_preco(self, app, client):
        p = _produto(preco=120)
        r = client.post("/catalogo/api/resumo", json={
            "itens": [{"tipo": "produto", "id": p, "quantidade": 2, "preco": 1}], "data": FESTA})
        corpo = r.get_json()
        assert corpo["total"] == 240 and corpo["itens"][0]["situacao"] == "disponivel"
        assert "livres" not in corpo["itens"][0]

    def test_nome_com_html_e_escapado(self, app, client):
        _produto("<img src=x onerror=alert(1)>")
        r = client.get("/catalogo")
        assert "<img src=x onerror" not in r.text

    def test_paginacao(self, app, client):
        for n in range(dados.VITRINE_POR_PAGINA + 3):
            _produto(f"Peça {n:02d}")
        r = client.get("/catalogo/pecas?pagina=2")
        assert r.status_code == 200 and 'aria-current="page">2<' in r.text


# ---------------------------------------------------------------------------
# Administração: permissões, auditoria, duplicar, ativar
# ---------------------------------------------------------------------------

class TesteAdministracao:
    def test_comercial_nao_altera_estoque_nem_regras(self, app):
        p = _produto(qtd=5)
        _usuario("vend", "comercial")
        c = _entrar(app, "vend")
        r = c.post(f"/produto/{p}", data={"nome": "Mesa Nova", "preco_locacao": "60",
                                          "quantidade_total": "999", "dias_uso": "9",
                                          "status": "inativo"})
        assert r.status_code == 302
        prod = dados.buscar_produto(p)
        assert prod["nome"] == "Mesa Nova" and prod["preco_locacao"] == 60
        assert prod["quantidade_total"] == 5 and prod["dias_uso"] == 2
        assert prod["status"] == "disponivel"

    def test_operacional_nao_edita_catalogo(self, app):
        p = _produto()
        _usuario("oper", "operacional")
        c = _entrar(app, "oper")
        assert c.post(f"/produto/{p}", data={"nome": "X"}).status_code == 403
        assert c.post(f"/catalogo-admin/produto/{p}/ativo", data={"ativo": "0"}).status_code == 403

    def test_vitrine_publica_nao_exige_login(self, app, client):
        _produto("Livre")
        assert client.get("/catalogo").status_code == 200
        assert client.get("/produtos").status_code in (302, 303)

    def test_ativar_inativar_com_auditoria(self, app, admin):
        p = _produto()
        r = admin.post(f"/catalogo-admin/produto/{p}/ativo", data={"ativo": "0"},
                       headers={"X-Requested-With": "fetch"})
        assert r.get_json()["ok"]
        assert dados.buscar_produto(p)["status"] == "inativo"
        reg = [a for a in dados.listar_audit(20) if a["entidade"] == "produto" and a["entidade_id"] == p]
        assert any("disponivel" in (a["dados"] or "") and "inativo" in (a["dados"] or "") for a in reg)

    def test_edicao_grava_antes_e_depois(self, app, admin):
        p = _produto(preco=50)
        admin.post(f"/produto/{p}", data={"nome": "Mesa Safari", "preco_locacao": "75",
                                          "quantidade_total": "5", "status": "disponivel"})
        reg = [a for a in dados.listar_audit(20) if a["tipo"] == "produto_alterou"][0]
        mud = json.loads(reg["dados"])["mudancas"]
        assert mud["preco_locacao"] == [50, 75]

    def test_kit_sem_pecas_nao_ativa(self, app):
        k = _kit("Kit Sem Nada", status="inativo")
        with pytest.raises(ValueError):
            dados.definir_ativo("kit", k, True)

    def test_duplicar_cria_copia_inativa_sem_mexer_no_original(self, app, admin):
        a = _produto("Painel")
        k = _kit("Kit Princesas", componentes=[(a, 2)])
        r = admin.post(f"/catalogo-admin/kit/{k}/duplicar")
        assert r.status_code == 302
        copia = [x for x in dados.listar_kits() if x["id"] != k][0]
        assert copia["status"] == "inativo" and not copia["publicado"]
        assert copia["slug"] != dados.buscar_kit(k)["slug"]
        assert [i["produto_id"] for i in dados.buscar_kit(copia["id"])["itens"]] == [a]
        assert dados.buscar_kit(k)["status"] == "ativo"

    def test_slug_unico_e_reservado(self, app):
        a = _produto("Kits")
        b = _produto("Mesa")
        c = _produto("Mesa")
        slugs = {dados.buscar_produto(i)["slug"] for i in (a, b, c)}
        assert len(slugs) == 3 and "kits" not in slugs

    def test_slug_invalido_recusado(self, app):
        with pytest.raises(dados.ErroDeCampo):
            dados.salvar_produto({"nome": "Mesa", "quantidade_total": 1, "slug": "com espaço/barra"})

    def test_listas_e_abas_da_administracao(self, app, admin):
        a = _produto("Peça Ativa")
        b = _produto("Peça Parada")
        dados.definir_ativo("produto", b, False)
        _kit("Kit Ativo", componentes=[(a, 1)])
        r = admin.get("/produtos?aba=inativos")
        assert "Peça Parada" in r.text and "Peça Ativa" not in r.text
        r = admin.get("/produtos?aba=kits")
        assert "Kit Ativo" in r.text and "Peça Ativa" not in r.text
        r = admin.get("/produtos?aba=todos&q=parada")
        assert "Peça Parada" in r.text and "Kit Ativo" not in r.text

    def test_pagina_de_edicao_tem_abas(self, app, admin):
        a = _produto("Painel")
        k = _kit("Kit Abas", componentes=[(a, 1)])
        r = admin.get(f"/kit/{k}?aba=disponibilidade")
        for texto in ("Informações", "Componentes", "Imagens", "Disponibilidade", "SEO"):
            assert texto in r.text
        assert "ca-dia--disponivel" in r.text

    def test_categoria_com_imagem_e_visibilidade(self, app, admin):
        from io import BytesIO
        from PIL import Image
        cat = dados.salvar_categoria("Personagens")
        buf = BytesIO()
        Image.new("RGB", (60, 40), "purple").save(buf, "PNG")
        r = admin.post(f"/categoria/{cat}/imagem",
                       data={"imagem": (BytesIO(buf.getvalue()), "cat.png")},
                       content_type="multipart/form-data")
        assert r.status_code == 302
        assert [c for c in dados.listar_categorias() if c["id"] == cat][0]["imagem"]
        falso = admin.post(f"/categoria/{cat}/imagem",
                           data={"imagem": (BytesIO(b"<?php echo 1; ?>"), "x.png")},
                           content_type="multipart/form-data", follow_redirects=True)
        assert "imagem" in falso.text.lower()
