"""Sprint 5.1 — produtos, serviços e kits (critérios de aceite).

Locação com estoque, balões sob encomenda por metro ou unidade com receita
de materiais, serviços de montagem, kits mistos com preço por componentes
ou fechado, modalidades de atendimento, migração segura e preservação de
pedidos e orçamentos.
"""

import json
import os
import sqlite3
from datetime import date, timedelta

import pytest

from sistema import dados, regras_itens as regras

FESTA = (date.today() + timedelta(days=30)).isoformat()
VESPERA = (date.today() + timedelta(days=29)).isoformat()


def _locacao(nome="Suporte cilíndrico", qtd=4, preco=60, **extra):
    return dados.salvar_produto(dict({"nome": nome, "tipo": "locacao", "quantidade_total": qtd,
                                      "preco_locacao": preco, "status": "disponivel"}, **extra))


def _guirlanda(nome="Guirlanda de balões", unidade="metro", preco=80, **extra):
    return dados.salvar_produto(dict({"nome": nome, "tipo": "encomenda", "unidade": unidade,
                                      "preco_locacao": preco, "status": "disponivel",
                                      "prazo_producao_dias": 3}, **extra))


def _montagem(nome="Montagem no local", preco=150, **extra):
    return dados.salvar_produto(dict({"nome": nome, "tipo": "servico", "unidade": "servico",
                                      "preco_locacao": preco, "status": "disponivel",
                                      "local_execucao": "evento", "exige_agendamento": 1}, **extra))


def _material(nome, unidade="un", arred="inteiro_acima", custo=None):
    return dados.salvar_material({"nome": nome, "unidade": unidade, "arredondamento": arred,
                                  "custo_referencia": custo})


def _cliente(nome="Cliente 51"):
    return dados.salvar_cliente({"nome": nome, "whatsapp": ""})


def _kit_misto(modo="fechado", preco=400):
    s, g, m = _locacao(), _guirlanda(), _montagem()
    k = dados.salvar_kit({"nome": "Kit Festa Essencial", "preco": preco, "status": "ativo",
                          "modo_preco": modo})
    dados.adicionar_item_kit(k, s, 2)
    dados.adicionar_item_kit(k, g, 2.5)
    dados.adicionar_item_kit(k, m, 1)
    return k, s, g, m


# ---------------------------------------------------------------------------
# Cadastro
# ---------------------------------------------------------------------------

class TesteCadastro:
    def test_cria_produto_de_locacao(self, app):
        p = dados.buscar_produto(_locacao())
        assert (p["tipo"], p["unidade"], p["quantidade_total"]) == ("locacao", "unidade", 4)

    def test_cria_baloes_por_metro_e_por_unidade(self, app):
        m = dados.buscar_produto(_guirlanda(qtd_minima="1,5"))
        u = dados.buscar_produto(_guirlanda("Bouquet de balões", unidade="unidade"))
        assert (m["unidade"], m["qtd_minima"], m["prazo_producao_dias"]) == ("metro", 1.5, 3)
        assert u["unidade"] == "unidade"
        assert m["quantidade_total"] == 0  # sob encomenda não tem estoque de peça pronta

    def test_cria_servico_de_montagem(self, app):
        s = dados.buscar_produto(_montagem(tempo_execucao_min=120, permite_retirada=1))
        assert (s["tipo"], s["unidade"], s["local_execucao"]) == ("servico", "servico", "evento")
        assert s["permite_retirada"] == 0  # serviço no evento não existe na retirada
        assert s["exige_agendamento"] == 1 and s["tempo_execucao_min"] == 120

    def test_edita_dados_e_preco_com_auditoria(self, app):
        g = _guirlanda()
        dados.salvar_produto({"nome": "Guirlanda safari", "tipo": "encomenda", "unidade": "metro",
                              "preco_locacao": "95,50", "status": "disponivel"}, g, usuario_id=None)
        p = dados.buscar_produto(g)
        assert (p["nome"], p["preco_locacao"]) == ("Guirlanda safari", 95.5)
        reg = [a for a in dados.listar_audit(10) if a["tipo"] == "produto_alterou"][0]
        assert json.loads(reg["dados"])["mudancas"]["preco_locacao"] == [80.0, 95.5]

    def test_desativar_preserva_historico(self, app):
        g = _guirlanda()
        ped = dados.salvar_pedido_festas(
            {"cliente_id": _cliente(), "data_evento": FESTA},
            [{"tipo": "produto", "item_id": g, "descricao": "Guirlanda", "quantidade": "2,5",
              "preco_unitario": 80}])
        dados.definir_ativo("produto", g, False)
        item = dados.buscar_pedido_festas(ped)["itens"][0]
        assert (item["item_id"], item["quantidade"], item["preco_unitario"]) == (g, 2.5, 80)
        assert dados.buscar_produto(g)["status"] == "inativo"

    @pytest.mark.parametrize("dados_, campo", [
        ({"nome": "", "tipo": "locacao"}, "nome"),
        ({"nome": "X", "tipo": ""}, "tipo"),
        ({"nome": "X", "tipo": "foguete"}, "tipo"),
        ({"nome": "X", "tipo": "locacao", "unidade": "metro"}, "unidade"),
        ({"nome": "X", "tipo": "servico", "unidade": "metro"}, "unidade"),
        ({"nome": "X", "tipo": "encomenda", "unidade": "hora"}, "unidade"),
        ({"nome": "X", "tipo": "locacao", "preco_locacao": "-1"}, "preco_locacao"),
        ({"nome": "X", "tipo": "encomenda", "unidade": "unidade", "qtd_minima": "1,5"}, "qtd_minima"),
        ({"nome": "X", "tipo": "locacao", "permite_retirada": 0, "permite_montagem": 0},
         "permite_retirada"),
    ])
    def test_validacoes_por_tipo(self, app, dados_, campo):
        with pytest.raises(dados.ErroDeCampo) as e:
            dados.salvar_produto(dados_)
        assert e.value.campo == campo

    def test_formulario_mostra_erro_ao_lado_do_campo(self, app, admin):
        r = admin.post("/produto", data={"modelo_51": "1", "nome": "Sem tipo", "tipo": "",
                                         "preco_locacao": "10"})
        assert r.status_code == 200 and 'id="campo-tipo"' in r.text and "Escolha o tipo" in r.text

    def test_formulario_cria_cada_tipo(self, app, admin):
        for tipo, unidade in (("locacao", "unidade"), ("encomenda", "metro"), ("servico", "hora")):
            r = admin.post("/produto", data={"modelo_51": "1", "nome": f"Item {tipo}", "tipo": tipo,
                                             "unidade": unidade, "preco_locacao": "10",
                                             "permite_montagem": "1", "status": "disponivel"})
            assert r.status_code == 302, tipo
        tipos = {p["nome"]: (p["tipo"], p["unidade"]) for p in dados.listar_produtos()}
        assert tipos["Item servico"] == ("servico", "hora")


# ---------------------------------------------------------------------------
# Receita de materiais
# ---------------------------------------------------------------------------

class TesteReceita:
    def test_cadastra_receita_e_calcula_consumo_fracionado(self, app):
        g = _guirlanda()
        balao = _material("Balão 9 pol.", custo=0.5)
        fita = _material("Fita de cetim", unidade="m", arred="exato", custo=1.2)
        dados.salvar_linha_receita(g, balao, 18)
        dados.salvar_linha_receita(g, fita, "0,35")
        c = dados.consumo_previsto_produto(g, "2,5")
        por = {m["nome"]: m for m in c["materiais"]}
        assert por["Balão 9 pol."]["calculado"] == 45 and por["Balão 9 pol."]["previsto"] == 45
        assert por["Fita de cetim"]["previsto"] == 0.875
        assert c["custo_total"] == round(45 * 0.5 + 0.875 * 1.2, 2)

    def test_arredonda_so_materiais_inteiros(self, app):
        g = _guirlanda()
        dados.salvar_linha_receita(g, _material("Balão 5 pol."), 12.5)
        dados.salvar_linha_receita(g, _material("Fio", unidade="m", arred="exato"), 1.1)
        por = {m["nome"]: m["previsto"] for m in dados.consumo_previsto_produto(g, 2.5)["materiais"]}
        assert por == {"Balão 5 pol.": 32, "Fio": 2.75}  # 31,25 -> 32; o fio fica exato

    def test_receita_so_para_encomenda_e_valores_validos(self, app):
        balao = _material("Balão")
        with pytest.raises(dados.ErroDeCampo):
            dados.salvar_linha_receita(_locacao(), balao, 1)
        g = _guirlanda()
        for invalido in ("0", "-3", "abc"):
            with pytest.raises(dados.ErroDeCampo):
                dados.salvar_linha_receita(g, balao, invalido)
        with pytest.raises(dados.ErroDeCampo):
            dados.consumo_previsto_produto(_guirlanda("Bouquet", unidade="unidade"), "1,5")

    def test_consultar_e_orcar_nao_movimenta_material(self, app):
        g = _guirlanda()
        balao = _material("Balão")
        dados.registrar_movimento_material(balao, "entrada", 100, "compra")
        dados.salvar_linha_receita(g, balao, 10)
        dados.consumo_previsto_produto(g, 3)
        dados.salvar_orcamento({"cliente_id": _cliente()},
                               [{"tipo": "produto", "item_id": g, "descricao": "Guirlanda",
                                 "quantidade": 3, "preco_unitario": 80}])
        with dados.conectar() as conn:
            movs = conn.execute("SELECT tipo FROM movimentos_material").fetchall()
        assert [m[0] for m in movs] == ["entrada"]
        assert dados.buscar_material(balao)["saldo"] == 100

    def test_pedido_mostra_consumo_previsto_para_o_sprint6(self, app):
        k, s, g, m = _kit_misto()
        balao = _material("Balão")
        dados.salvar_linha_receita(g, balao, 10)
        ped = dados.salvar_pedido_festas(
            {"cliente_id": _cliente(), "data_evento": FESTA, "data_retirada": VESPERA,
             "data_devolucao": FESTA},
            [{"tipo": "kit", "item_id": k, "descricao": "Kit", "quantidade": 2, "preco_unitario": 400}])
        consumo = dados.consumo_previsto_pedido(ped)
        assert consumo[0]["nome"] == "Balão" and consumo[0]["previsto"] == 50  # 2 kits x 2,5 m x 10

    def test_movimentacoes_rastreaveis(self, app, admin):
        balao = _material("Balão")
        dados.registrar_movimento_material(balao, "entrada", 200, "compra")
        dados.registrar_movimento_material(balao, "perda", 5, "estourou")
        dados.registrar_movimento_material(balao, "ajuste", -3, "inventário")
        assert dados.buscar_material(balao)["saldo"] == 192
        with pytest.raises(dados.ErroDeCampo):  # reserva e baixa: só pelo fluxo do Sprint 6
            dados.registrar_movimento_material(balao, "baixa", 1)
        with pytest.raises(dados.ErroDeCampo):  # perda sem motivo
            dados.registrar_movimento_material(balao, "perda", 1)
        with pytest.raises(dados.ErroDeCampo):  # balão é inteiro
            dados.registrar_movimento_material(balao, "entrada", 1.5)
        assert any(a["tipo"] == "material_movimento" and '"perda"' in a["dados"]
                   for a in dados.listar_audit(20))


# ---------------------------------------------------------------------------
# Kits
# ---------------------------------------------------------------------------

class TesteKits:
    def test_kit_misto_e_preco_de_referencia(self, app):
        k, *_ = _kit_misto()
        r = dados.resumo_kit(k)
        assert sorted(c["tipo"] for c in r["componentes"]) == ["encomenda", "locacao", "servico"]
        assert r["referencia"]["obrigatorios"] == 2 * 60 + 2.5 * 80 + 150  # 470
        assert r["preco_final"] == 400
        assert r["diferenca"] == {"valor": 70.0, "percentual": 14.9, "desconto": True}

    def test_alterar_quantidade_recalcula(self, app):
        k, s, g, m = _kit_misto(modo="componentes")
        assert dados.buscar_kit(k)["preco"] == 470
        dados.adicionar_item_kit(k, g, 4)
        assert dados.resumo_kit(k)["referencia"]["obrigatorios"] == 590
        assert dados.buscar_kit(k)["preco"] == 590  # modo componentes acompanha

    def test_preco_do_componente_atualiza_kit_por_componentes(self, app):
        k, s, g, m = _kit_misto(modo="componentes")
        dados.salvar_produto({"nome": "Montagem no local", "tipo": "servico", "unidade": "servico",
                              "preco_locacao": 200, "status": "disponivel"}, m)
        assert dados.buscar_kit(k)["preco"] == 520

    def test_preco_fechado_nao_e_imposto_pela_soma(self, app):
        k, s, g, m = _kit_misto(modo="fechado", preco=999)
        dados.adicionar_item_kit(k, g, 1)
        assert dados.buscar_kit(k)["preco"] == 999
        assert dados.resumo_kit(k)["diferenca"]["desconto"] is False

    def test_opcional_fica_fora_da_referencia_e_do_estoque(self, app):
        k, s, g, m = _kit_misto(modo="componentes")
        extra = _locacao("Bandeja", qtd=1, preco=30)
        dados.adicionar_item_kit(k, extra, 1, obrigatorio=False)
        r = dados.resumo_kit(k)
        assert r["referencia"] == {"obrigatorios": 470, "opcionais": 30, "total": 500}
        dados.salvar_pedido_festas(
            {"cliente_id": _cliente(), "data_evento": FESTA, "data_retirada": VESPERA,
             "data_devolucao": FESTA},
            [{"tipo": "kit", "item_id": k, "descricao": "Kit", "quantidade": 1, "preco_unitario": 470}])
        assert dados.situacao_na_data("produto", extra, FESTA)["livres"] == 1

    def test_componente_inativo_nao_entra_em_kit_novo(self, app):
        k = dados.salvar_kit({"nome": "Kit", "preco": 100})
        g = _guirlanda()
        dados.definir_ativo("produto", g, False)
        with pytest.raises(dados.ErroDeCampo):
            dados.adicionar_item_kit(k, g, 1)

    def test_quantidade_do_componente_respeita_a_unidade(self, app):
        k = dados.salvar_kit({"nome": "Kit", "preco": 100})
        with pytest.raises(dados.ErroDeCampo):
            dados.adicionar_item_kit(k, _locacao(), 1.5)
        dados.adicionar_item_kit(k, _guirlanda(), "1,75")
        assert dados.resumo_kit(k)["componentes"][0]["quantidade"] == 1.75

    def test_kit_nao_contem_kit(self, app):
        """Componentes apontam para produtos: um kit não entra em outro (sem ciclo)."""
        k1 = dados.salvar_kit({"nome": "Kit A", "preco": 100})
        k2 = dados.salvar_kit({"nome": "Kit B", "preco": 100})
        with dados.conectar() as conn:
            fk = [r[2] for r in conn.execute("PRAGMA foreign_key_list(itens_kit)") if r[3] == "produto_id"]
        assert fk == ["produtos"]
        with pytest.raises(dados.ErroDeCampo):
            dados.adicionar_item_kit(k1, 999999, 1)  # id de kit não é produto
        assert dados.resumo_kit(k2)["componentes"] == []

    def test_disponibilidade_do_kit_misto(self, app):
        k, s, g, m = _kit_misto()
        # suporte: 4 em estoque, 2 por kit -> 2 kits; balões e montagem não limitam o estoque
        assert dados.situacao_na_data("kit", k, FESTA)["capacidade"] == 2
        # prazo de produção da guirlanda (3 dias) vale para o kit
        amanha = (date.today() + timedelta(days=1)).isoformat()
        assert dados.situacao_na_data("kit", k, amanha)["situacao"] == "indisponivel"

    def test_encomenda_nao_ocupa_estoque(self, app):
        g = _guirlanda()
        for _ in range(3):
            dados.salvar_pedido_festas(
                {"cliente_id": _cliente(), "data_evento": FESTA},
                [{"tipo": "produto", "item_id": g, "descricao": "G", "quantidade": 10,
                  "preco_unitario": 80}])
        sit = dados.situacao_na_data("produto", g, FESTA)
        assert sit["situacao"] == "disponivel" and sit["livres"] is None

    def test_mudar_kit_nao_muda_pedido_antigo(self, app):
        k, s, g, m = _kit_misto()
        ped = dados.salvar_pedido_festas(
            {"cliente_id": _cliente(), "data_evento": FESTA, "data_retirada": VESPERA,
             "data_devolucao": FESTA},
            [{"tipo": "kit", "item_id": k, "descricao": "Kit Festa", "quantidade": 1,
              "preco_unitario": 400}])
        outro = _locacao("Bandeja", qtd=1)
        dados.adicionar_item_kit(k, outro, 1)
        dados.salvar_kit({"nome": "Kit Festa Essencial", "preco": 999, "status": "ativo"}, k)
        # a bandeja entrou no kit depois: o pedido antigo não a ocupa
        assert dados.situacao_na_data("produto", outro, FESTA)["livres"] == 1
        linha = dados.buscar_pedido_festas(ped)["itens"][0]
        assert linha["preco_unitario"] == 400
        assert {c["produto_id"] for c in json.loads(linha["composicao"])} == {s, g, m}
        # editar o pedido mantém a composição da venda
        dados.salvar_pedido_festas(
            {"cliente_id": linha and dados.buscar_pedido_festas(ped)["cliente_id"],
             "data_evento": FESTA, "data_retirada": VESPERA, "data_devolucao": FESTA,
             "observacoes": "editado"},
            [{"tipo": "kit", "item_id": k, "descricao": "Kit Festa", "quantidade": 1,
              "preco_unitario": 400}], ped)
        assert dados.situacao_na_data("produto", outro, FESTA)["livres"] == 1

    def test_tela_de_composicao(self, app, admin):
        k, *_ = _kit_misto()
        r = admin.get(f"/kit/{k}?aba=componentes")
        for texto in ("Sob encomenda", "Serviço", "Locação", "Referência (obrigatórios)",
                      "Desconto sobre a referência", 'value="2.5"'):
            assert texto in r.text, texto
        r = admin.post(f"/kit/{k}/item", data={"produto_id": _guirlanda("Arco"), "quantidade": "1.5",
                                               "obrigatorio": "0"})
        assert r.status_code == 302
        assert any(not c["obrigatorio"] for c in dados.resumo_kit(k)["componentes"])


# ---------------------------------------------------------------------------
# Modalidades
# ---------------------------------------------------------------------------

class TesteModalidades:
    def test_produto_com_uma_ou_as_duas(self, app):
        so_retirada = _guirlanda(permite_montagem=0)
        ambas = _guirlanda("Bouquet", unidade="unidade")
        assert regras.modalidades_do_produto(dados.buscar_produto(so_retirada)) == {"retirada"}
        assert regras.modalidades_do_produto(dados.buscar_produto(ambas)) == {"retirada", "montagem"}

    def test_kit_com_montagem_nao_aceita_retirada(self, app):
        k, *_ = _kit_misto()
        r = dados.resumo_kit(k)
        assert r["modalidades"] == ["montagem"] and r["restricoes"]
        cli = _cliente()
        with pytest.raises(dados.ErroDeCampo) as e:
            dados.salvar_pedido_festas(
                {"cliente_id": cli, "data_evento": FESTA, "modalidade": "retirada"},
                [{"tipo": "kit", "item_id": k, "descricao": "Kit", "quantidade": 1,
                  "preco_unitario": 400}])
        assert e.value.campo == "modalidade" and "Montagem no local" in str(e.value)
        ped = dados.salvar_pedido_festas(
            {"cliente_id": cli, "data_evento": FESTA, "modalidade": "montagem"},
            [{"tipo": "kit", "item_id": k, "descricao": "Kit", "quantidade": 1, "preco_unitario": 400}])
        assert dados.buscar_pedido_festas(ped)["modalidade"] == "montagem"

    def test_orcamento_valida_e_conversao_leva_a_modalidade(self, app):
        g = _guirlanda(permite_montagem=0)
        cli = _cliente()
        with pytest.raises(dados.ErroDeCampo):
            dados.salvar_orcamento({"cliente_id": cli, "modalidade": "montagem"},
                                   [{"tipo": "produto", "item_id": g, "descricao": "G",
                                     "quantidade": 2, "preco_unitario": 80}])
        orc = dados.salvar_orcamento({"cliente_id": cli, "modalidade": "retirada", "data_evento": FESTA},
                                     [{"tipo": "produto", "item_id": g, "descricao": "G",
                                       "quantidade": "2,5", "preco_unitario": 80}])
        ped = dados.converter_orcamento_em_pedido(orc)
        p = dados.buscar_pedido_festas(ped)
        assert p["modalidade"] == "retirada" and p["itens"][0]["quantidade"] == 2.5
        assert p["itens"][0]["unidade"] == "metro"

    def test_formulario_sem_campo_mantem_modalidade(self, app):
        cli = _cliente()
        g = _guirlanda()
        itens = [{"tipo": "produto", "item_id": g, "descricao": "G", "quantidade": 1,
                  "preco_unitario": 80}]
        ped = dados.salvar_pedido_festas({"cliente_id": cli, "data_evento": FESTA,
                                          "modalidade": "montagem"}, itens)
        dados.salvar_pedido_festas({"cliente_id": cli, "data_evento": FESTA, "modalidade": None},
                                   itens, ped)
        assert dados.buscar_pedido_festas(ped)["modalidade"] == "montagem"


# ---------------------------------------------------------------------------
# Pedidos, orçamentos e quantidade fracionada
# ---------------------------------------------------------------------------

class TesteVendas:
    def test_fracao_so_onde_a_unidade_permite(self, app):
        cli = _cliente()
        with pytest.raises(dados.ErroDeCampo):
            dados.salvar_pedido_festas({"cliente_id": cli, "data_evento": FESTA},
                                       [{"tipo": "produto", "item_id": _locacao(), "descricao": "S",
                                         "quantidade": 1.5, "preco_unitario": 10}])
        ped = dados.salvar_pedido_festas({"cliente_id": cli, "data_evento": FESTA},
                                         [{"tipo": "produto", "item_id": _guirlanda(), "descricao": "G",
                                           "quantidade": "2,5", "preco_unitario": 80}])
        assert dados.buscar_pedido_festas(ped)["total"] == 200

    def test_valores_gravados_nao_mudam_com_o_cadastro(self, app):
        g = _guirlanda(preco=80)
        orc = dados.salvar_orcamento({"cliente_id": _cliente()},
                                     [{"tipo": "produto", "item_id": g, "descricao": "G",
                                       "quantidade": 2, "preco_unitario": 80}])
        dados.salvar_produto({"nome": "Guirlanda de balões", "tipo": "encomenda", "unidade": "metro",
                              "preco_locacao": 120, "status": "disponivel"}, g)
        assert dados.buscar_orcamento(orc)["itens"][0]["preco_unitario"] == 80

    def test_vitrine_aceita_metros(self, app):
        g = _guirlanda(publicado=1, qtd_minima=1.5)
        r = dados.resumo_orcamento_publico([{"tipo": "produto", "id": g, "quantidade": "1"}], FESTA)
        assert r["itens"][0]["quantidade"] == 1.5  # sobe até a quantidade mínima
        r = dados.resumo_orcamento_publico([{"tipo": "produto", "id": g, "quantidade": 2.5}])
        assert r["itens"][0]["subtotal"] == 200 and r["itens"][0]["sigla"] == "m"

    def test_formulario_do_pedido_tem_modalidade_e_catalogo(self, app, admin):
        _guirlanda()
        r = admin.get("/pedido/novo")
        assert 'name="modalidade"' in r.text and 'id="catalogo-venda"' in r.text
        assert "Guirlanda de balões" in r.text


# ---------------------------------------------------------------------------
# Migração e classificação dos produtos existentes
# ---------------------------------------------------------------------------

class TesteMigracao:
    def _banco_antigo(self, caminho):
        """Banco com o esquema anterior ao 5.1 (sem as colunas novas)."""
        dados.CAMINHO_BD = caminho
        dados._tenant_padrao_cache.clear()
        dados.inicializar()
        with dados.usando_tenant(dados.tenant_padrao()):
            p = dados.salvar_produto({"nome": "Arco de balões", "quantidade_total": 2,
                                      "preco_locacao": 120})
            dados.salvar_pedido_festas({"cliente_id": _cliente(), "data_evento": FESTA},
                                       [{"tipo": "produto", "item_id": p, "descricao": "Arco",
                                         "quantidade": 1, "preco_unitario": 120}])
        conn = sqlite3.connect(caminho)
        conn.execute("DROP INDEX IF EXISTS ix_produtos_tipo")
        for tabela, cols in (("produtos", dados._CAMPOS_51_PRODUTO),
                             ("kits", ("codigo_sku", "modo_preco", "permite_retirada", "permite_montagem")),
                             ("itens_kit", ("obrigatorio",)), ("pedidos", ("modalidade",)),
                             ("orcamentos", ("modalidade",)),
                             ("itens_pedido", ("unidade", "composicao")),
                             ("itens_orcamento", ("unidade", "composicao"))):
            for c in cols:
                conn.execute(f"ALTER TABLE {tabela} DROP COLUMN {c}")
        for t in ("movimentos_material", "receitas_produto", "materiais"):
            conn.execute(f"DROP TABLE {t}")
        conn.commit()
        conn.close()
        return p

    def test_migra_com_backup_e_sem_classificar_sozinho(self, app, tmp_path):
        caminho = str(tmp_path / "antigo.db")
        p = self._banco_antigo(caminho)
        with dados.conectar() as conn:
            antes = dados._retrato_51(conn)
        dados.inicializar()  # faz o backup sozinho antes de migrar
        backups = [f for f in os.listdir(tmp_path) if "backup-antes-sprint51" in f]
        assert len(backups) == 1
        with dados.conectar() as conn:
            assert dados._retrato_51(conn) == antes
        prod = dados.buscar_produto(p)
        assert prod["tipo"] is None and prod["unidade"] == "unidade"  # a classificar
        # continua funcionando como locação: o pedido ocupa a unidade
        assert dados.situacao_na_data("produto", p, FESTA)["livres"] == 1
        # backup tem os dados de antes
        b = sqlite3.connect(str(tmp_path / backups[0]))
        assert b.execute("SELECT COUNT(*) FROM produtos").fetchone()[0] == 1
        # segunda inicialização não faz outro backup nem mexe em nada
        dados.backup_antes_sprint51()
        dados.inicializar()
        assert len([f for f in os.listdir(tmp_path) if "backup-antes-sprint51" in f]) == 1
        assert any(a["tipo"] == "migracao_51" for a in dados.listar_audit(10))

    def test_classificar_em_lote(self, app, admin):
        with dados.conectar() as conn:
            conn.execute("INSERT INTO produtos (tenant_id, codigo_sku, nome, criado_em, atualizado_em)"
                         " VALUES (?, 'A-1', 'Guirlanda antiga', '2026-01-01', '2026-01-01')",
                         (dados.tenant_padrao(),))
            pid = conn.execute("SELECT id FROM produtos WHERE codigo_sku = 'A-1'").fetchone()[0]
        r = admin.get("/produtos?aba=classificar")
        assert "Guirlanda antiga" in r.text and "sugestão: Sob encomenda" in r.text
        admin.post("/catalogo-admin/classificar", data={"ids": [pid], "tipo": "encomenda"})
        assert dados.buscar_produto(pid)["tipo"] == "encomenda"
        assert "Guirlanda antiga" not in admin.get("/produtos?aba=classificar").text

    def test_sugestao_marca_ambiguidade(self):
        assert regras.sugerir_tipo("Arco de balões com montagem")[0] == "ambiguo"
        assert regras.sugerir_tipo("Bandeja dourada")[0] == "locacao"


# ---------------------------------------------------------------------------
# API, permissões e telas
# ---------------------------------------------------------------------------

class TesteApiETelas:
    def test_api_preco_e_consumo(self, app, admin):
        k, s, g, m = _kit_misto()
        dados.salvar_linha_receita(g, _material("Balão"), 10)
        r = admin.get(f"/api/catalogo/kit/{k}/preco").get_json()
        assert r["preco_final"] == 400 and r["referencia"]["obrigatorios"] == 470
        r = admin.get(f"/api/catalogo/produto/{g}/consumo?quantidade=2.5").get_json()
        assert r["materiais"][0]["previsto"] == 25
        r = admin.get(f"/api/catalogo/produto/{s}/consumo?quantidade=1.5")
        assert r.status_code == 422 and r.get_json()["campo"] == "quantidade"
        itens = admin.get("/api/catalogo/itens?natureza=servico").get_json()["itens"]
        assert [i["nome"] for i in itens] == ["Montagem no local"]

    def test_comercial_nao_gerencia_materiais_nem_prazo(self, app):
        from werkzeug.security import generate_password_hash
        dados.salvar_usuario({"nome": "Vend", "login": "vend51", "perfil": "comercial"},
                             senha_hash=generate_password_hash("123"))
        c = app.test_client()
        c.post("/entrar", data={"login": "vend51", "senha": "123"})
        assert c.get("/materiais").status_code == 200
        assert c.post("/materiais", data={"nome": "X", "unidade": "un"}).status_code == 403
        g = _guirlanda()
        c.post(f"/produto/{g}", data={"modelo_51": "1", "nome": "Guirlanda de balões",
                                      "tipo": "encomenda", "unidade": "metro", "preco_locacao": "80",
                                      "prazo_producao_dias": "0", "permite_retirada": "1"})
        assert dados.buscar_produto(g)["prazo_producao_dias"] == 3

    def test_telas_carregam(self, app, admin):
        g = _guirlanda()
        mat = _material("Balão")
        dados.salvar_linha_receita(g, mat, 10)
        for url, texto in ((f"/produto/{g}", "Tipo do item"), (f"/produto/{g}?aba=receita&simular=2.5", "25 un"),
                           ("/materiais", "Balão"), (f"/material/{mat}", "Usado nas receitas"),
                           ("/produtos?natureza=encomenda", "Sob encomenda")):
            r = admin.get(url)
            assert r.status_code == 200 and texto in r.text, url
