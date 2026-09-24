"""Sprint 2 — módulo de Pedidos: lista, detalhe, transições, auditoria."""

import json
import re

import pytest

from sistema import dados, formato


def _cliente(nome="Juliana Mello", whatsapp=""):
    return dados.salvar_cliente({"nome": nome, "whatsapp": whatsapp})


def _itens(servicos=(), kit="Kit Safari", preco=320):
    itens = [{"tipo": "kit", "descricao": kit, "quantidade": 1, "preco_unitario": preco},
             {"tipo": "produto", "descricao": "Bandejas decorativas", "quantidade": 2,
              "preco_unitario": 40}]
    itens += [{"tipo": "servico", "descricao": s, "quantidade": 1, "preco_unitario": 0}
              for s in servicos]
    return itens


def _pedido(cliente_id, evento="2026-10-10", servicos=(), **extra):
    d = {"cliente_id": cliente_id, "data_evento": evento,
         "data_retirada": extra.pop("retirada", evento),
         "data_devolucao": extra.pop("devolucao", evento), "observacoes": ""}
    d.update(extra)
    return dados.salvar_pedido_festas(d, extra.pop("itens", None) or _itens(servicos))


def _status(pid):
    p = dados.buscar_pedido_festas(pid)
    return p["status_comercial"], p["status_operacional"]


def _entrar_como(admin, client, perfil):
    admin.post("/usuario", data={"nome": perfil.title(), "login": perfil,
                                 "senha": "senha123", "perfil": perfil, "ativo": "1"})
    admin.get("/sair")
    client.post("/entrar", data={"login": perfil, "senha": "senha123"})
    return client


def _eventos(pid):
    with dados.conectar() as conn:
        return [json.loads(r["dados"]) | {"usuario_id": r["usuario_id"]} for r in conn.execute(
            "SELECT dados, usuario_id FROM audit_log WHERE tipo='pedido_evento'"
            " AND json_extract(dados, '$.pedido_id') = ? ORDER BY id", (pid,))]


# --- Listagem --------------------------------------------------------------

class TesteListagem:
    def test_carrega_colunas_do_figma(self, app, admin):
        _pedido(_cliente(), servicos=["Entrega"])
        r = admin.get("/pedidos")
        for texto in ("Pedido", "Cliente", "Evento", "Itens", "Total", "Serviços",
                      "Origem", "Comercial", "Operacional", "Ações", "Limpar filtros",
                      "Novo pedido", "Mostrando 1 a 1 de 1 pedido"):
            assert texto in r.text

    def test_paginacao_no_banco(self, app, admin):
        cli = _cliente()
        for i in range(23):
            _pedido(cli, evento=f"2026-10-{i + 1:02d}")
        res = dados.consultar_pedidos(por_pagina=10, pagina=3)
        assert (res["total"], res["paginas"], len(res["registros"])) == (23, 3, 3)
        assert (res["inicio_item"], res["fim_item"]) == (21, 23)
        assert dados.consultar_pedidos(pagina=99)["pagina"] == 3
        assert dados.consultar_pedidos(por_pagina=7)["por_pagina"] == 10
        r = admin.get("/pedidos?pagina=2&por_pagina=20")
        assert "Mostrando 21 a 23 de 23 pedidos" in r.text

    @pytest.mark.parametrize("termo", ["thais", "Thaís", "#{id}", "{id}", "safari", "9999-0000"])
    def test_busca(self, app, termo):
        alvo = _pedido(_cliente("Thaís Oshita", "67999990000"))
        _pedido(_cliente("Outro Cliente", "11911112222"), itens=_itens(kit="Kit Futebol"))
        res = dados.consultar_pedidos(q=termo.format(id=alvo))
        assert [r["id"] for r in res["registros"]] == [alvo]

    def test_filtros_e_combinacao(self, app):
        cli = _cliente()
        a = _pedido(cli, evento="2026-10-05")
        b = _pedido(cli, evento="2026-11-05")
        dados.avancar_status_operacional(b)
        c = _pedido(cli, evento="2026-10-20")
        dados.cancelar_pedido(c, "desistiu")
        ids = lambda **f: {r["id"] for r in dados.consultar_pedidos(**f)["registros"]}
        assert ids(status_operacional="separado") == {b}
        assert ids(status_comercial="cancelado") == {c}
        assert ids(inicio="2026-10-01", fim="2026-10-31") == {a, c}
        assert ids(inicio="2026-10-01", fim="2026-10-31", status_comercial="confirmado") == {a}
        assert ids(origem="Morumbi Festas") == {a, b, c}
        assert ids(status_comercial="invalido") == {a, b, c}

    def test_abas_e_contagens(self, app):
        cli = _cliente()
        andamento = _pedido(cli, evento="2026-10-05")
        cancelado = _pedido(cli, evento="2026-10-06")
        dados.cancelar_pedido(cancelado)
        finalizado = _pedido(cli, evento="2026-10-07")
        for _ in range(5):
            dados.avancar_status_operacional(finalizado)
        dados.finalizar_pedido(finalizado)
        historico = _pedido(cli, evento="2025-01-10")
        with dados.conectar() as conn:
            dados.aplicar_normalizacao(conn)
            conn.execute("INSERT INTO eventos_historico (cliente_id, origem_id, origem,"
                         " data_evento, valor, status_origem) VALUES (?, 7, 'Formulario Festas',"
                         " '2024-05-05', 0, 'entregue')", (cli,))
        res = dados.consultar_pedidos()
        assert res["contagens"] == {"todos": 5, "andamento": 1, "finalizados": 3,
                                    "cancelados": 1, "historico": 2}
        aba = lambda nome: {(r["tipo"], r["id"]) for r in dados.consultar_pedidos(aba=nome)["registros"]}
        assert aba("andamento") == {("pedido", andamento)}
        assert ("pedido", historico) in aba("historico")
        assert ("pedido", finalizado) in aba("finalizados")

    def test_periodos_da_lista(self, app, admin):
        r = admin.get("/pedidos?periodo=personalizado&inicio=2026-12-01&fim=2026-01-01")
        assert "A data inicial deve ser anterior" in r.text
        for periodo in ("hoje", "semana", "este_mes", "mes_anterior", "este_ano"):
            assert admin.get(f"/pedidos?periodo={periodo}").status_code == 200

    def test_resposta_parcial_para_atualizar_sem_recarregar(self, app, admin):
        _pedido(_cliente())
        r = admin.get("/pedidos?parcial=1&aba=andamento")
        assert r.status_code == 200
        assert "<html" not in r.text and "data-estado" in r.text
        assert 'data-aba="andamento"' in r.text

    def test_estado_vazio(self, app, admin):
        r = admin.get("/pedidos?q=ninguem")
        assert "Nenhum pedido encontrado." in r.text
        assert "Tente alterar os filtros ou crie um novo pedido." in r.text


# --- Serviços --------------------------------------------------------------

class TesteServicos:
    @pytest.mark.parametrize("servicos", [
        ["Entrega"], ["Retirada", "Devolução"], ["Entrega", "Montagem", "Recolhimento"],
        ["Entrega", "Montagem", "Desmontagem", "Recolhimento", "Devolução"]])
    def test_chips_por_quantidade_de_servicos(self, app, admin, servicos):
        pid = _pedido(_cliente(), servicos=servicos)
        r = admin.get("/pedidos")
        linha = re.search(rf'#{pid}</a>.*?</tr>', r.text, re.S).group(0)
        chips = re.findall(r'<span class="ped-chip">([^<]+)</span>', linha)
        assert chips == servicos
        assert 'class="ped-chips"' in linha
        registro = dados.consultar_pedidos()["registros"][0]
        assert registro["itens"] == 3

    def test_sem_servico_mostra_traco(self, app, admin):
        _pedido(_cliente())
        assert '<span class="ped-nada">—</span>' in admin.get("/pedidos").text


# --- Status e transições ---------------------------------------------------

class TesteStatus:
    def test_fluxo_completo_com_eventos(self, app, admin):
        pid = _pedido(_cliente())
        for esperado in ("separado", "montado", "entregue", "recolhido", "conferido"):
            r = admin.post(f"/operacao/pedido/{pid}/avancar",
                           data={"voltar": f"/pedido/{pid}"})
            assert r.headers["Location"] == f"/pedido/{pid}"
            assert _status(pid) == ("confirmado", esperado)
        admin.post(f"/pedido/{pid}/finalizar", data={"voltar": f"/pedido/{pid}"})
        assert _status(pid) == ("finalizado", "finalizado")
        titulos = [e["titulo"] for e in _eventos(pid)]
        assert titulos == ["Pedido criado", "Itens separados", "Pedido montado",
                           "Retirada / entrega registrada", "Devolução registrada",
                           "Itens conferidos", "Pedido finalizado"]
        assert all(e["usuario_id"] == 1 for e in _eventos(pid)[1:])

    def test_finalizar_so_quando_conferido(self, app, admin):
        pid = _pedido(_cliente())
        r = admin.post(f"/pedido/{pid}/finalizar", follow_redirects=True)
        assert "Só é possível finalizar um pedido conferido." in r.text
        assert _status(pid) == ("confirmado", "preparacao")

    def test_transicoes_invalidas(self, app):
        pid = _pedido(_cliente())
        dados.cancelar_pedido(pid, "desistiu")
        with pytest.raises(ValueError, match="cancelado não pode avançar"):
            dados.avancar_status_operacional(pid)
        with pytest.raises(ValueError, match="já está cancelado"):
            dados.cancelar_pedido(pid)
        fin = _pedido(_cliente("Outro"))
        for _ in range(5):
            dados.avancar_status_operacional(fin)
        dados.finalizar_pedido(fin)
        with pytest.raises(ValueError, match="finalizado não pode ser cancelado"):
            dados.cancelar_pedido(fin)
        with pytest.raises(ValueError):
            dados.avancar_status_operacional(fin)

    def test_cancelamento_registra_motivo_usuario_e_libera_faturamento(self, app, admin):
        pid = _pedido(_cliente(), evento="2026-09-10")
        admin.post(f"/pedido/{pid}/cancelar", data={"motivo": "Cliente desistiu",
                                                    "voltar": f"/pedido/{pid}"})
        ev = _eventos(pid)[-1]
        assert ev["titulo"] == "Pedido cancelado" and ev["detalhe"] == "Cliente desistiu"
        assert ev["usuario_id"] == 1
        assert ev["mudancas"]["status_comercial"] == ["confirmado", "cancelado"]
        assert dados.faturamento_periodo("2026-09-01", "2026-09-30")["quantidade"] == 0
        r = admin.get(f"/pedido/{pid}")
        assert "Motivo do cancelamento" in r.text and "Cliente desistiu" in r.text

    def test_finalizado_entra_no_faturamento(self, app):
        pid = _pedido(_cliente(), evento="2026-08-10")
        for _ in range(5):
            dados.avancar_status_operacional(pid)
        dados.finalizar_pedido(pid)
        fat = dados.faturamento_periodo("2026-08-01", "2026-08-31")
        assert (fat["quantidade"], fat["total"]) == (1, 400.0)

    def test_ocorrencia(self, app, admin):
        pid = _pedido(_cliente())
        r = admin.post(f"/pedido/{pid}/ocorrencia", data={"texto": "  "}, follow_redirects=True)
        assert "Descreva a ocorrência." in r.text
        admin.post(f"/pedido/{pid}/ocorrencia", data={"texto": "Toalha rasgada"})
        assert "Toalha rasgada" in admin.get(f"/pedido/{pid}").text

    def test_redirecionamento_seguro(self, app, admin):
        pid = _pedido(_cliente())
        r = admin.post(f"/pedido/{pid}/ocorrencia",
                       data={"texto": "x", "voltar": "//exemplo.com/pedido"})
        assert r.headers["Location"] == f"/pedido/{pid}"


# --- Permissões ------------------------------------------------------------

class TestePermissoes:
    def test_operacional_nao_edita_nem_cancela(self, app, admin, client):
        pid = _pedido(_cliente())
        c = _entrar_como(admin, client, "operacional")
        assert c.post(f"/pedido/{pid}/cancelar").status_code == 403
        assert c.get(f"/pedido/{pid}/editar").status_code == 403
        r = c.get(f"/pedido/{pid}")
        assert "Marcar como separado" in r.text
        assert "Editar pedido" not in r.text and "Cancelar pedido" not in r.text

    def test_comercial_nao_avanca_operacao(self, app, admin, client):
        pid = _pedido(_cliente())
        c = _entrar_como(admin, client, "comercial")
        assert c.post(f"/operacao/pedido/{pid}/avancar").status_code == 403
        assert c.post(f"/pedido/{pid}/finalizar").status_code == 403
        r = c.get(f"/pedido/{pid}")
        assert "A próxima etapa é registrada pela equipe de operação." in r.text

    def test_comercial_nao_altera_status_pelo_formulario(self, app, admin, client):
        cli = _cliente()
        pid = _pedido(cli)
        c = _entrar_como(admin, client, "comercial")
        pagina = c.get(f"/pedido/{pid}/editar").text
        assert 'name="status_comercial"' not in pagina
        c.post(f"/pedido/{pid}/editar", data={
            "cliente_id": str(cli), "data_evento": "2026-10-10",
            "status_comercial": "finalizado", "status_operacional": "conferido",
            "item_tipo_0": "kit", "item_descricao_0": "Kit Safari",
            "item_quantidade_0": "1", "item_preco_0": "320"})
        assert _status(pid) == ("confirmado", "preparacao")


# --- Histórico -------------------------------------------------------------

class TesteHistorico:
    def _historico(self):
        pid = _pedido(_cliente(), evento="2025-03-03")
        with dados.conectar() as conn:
            dados.aplicar_normalizacao(conn)
        return pid

    def test_modo_consulta_sem_acoes(self, app, admin):
        pid = self._historico()
        r = admin.get(f"/pedido/{pid}")
        assert "Pedido histórico em modo consulta" in r.text
        for acao in ("Marcar como separado", "Cancelar pedido", "Registrar ocorrência",
                     "Editar pedido"):
            assert acao not in r.text
        assert "Correção administrativa" in r.text

    def test_nao_opera_nem_aparece_em_andamento(self, app, admin):
        pid = self._historico()
        with pytest.raises(ValueError):
            dados.avancar_status_operacional(pid)
        assert pid not in {r["id"] for r in dados.consultar_pedidos(aba="andamento")["registros"]}
        assert pid not in [p["id"] for p in dados.listar_pedidos_operacional()]
        assert pid in {r["id"] for r in dados.consultar_pedidos(aba="historico")["registros"]}

    def test_so_admin_corrige_e_fica_auditado(self, app, admin):
        pid = self._historico()
        ped = dados.buscar_pedido_festas(pid)
        base = {"cliente_id": ped["cliente_id"], "data_evento": "2025-03-04",
                "versao": ped["atualizado_em"]}
        with pytest.raises(dados.ErroDeCampo, match="somente um administrador"):
            dados.salvar_pedido_festas(dict(base), _itens(), pid, pode_alterar_status=False)
        dados.salvar_pedido_festas(dict(base), _itens(), pid, usuario_id=1)
        ev = _eventos(pid)[-1]
        assert ev["titulo"] == "Pedido editado"
        assert ev["mudancas"]["data_evento"] == ["2025-03-03", "2025-03-04"]

    def test_importado_abre_em_modo_consulta(self, app, admin):
        cli = _cliente()
        with dados.conectar() as conn:
            hid = conn.execute(
                "INSERT INTO eventos_historico (cliente_id, origem_id, origem, data_evento,"
                " descricao, valor, status_origem, criado_em) VALUES (?, 351,"
                " 'Formulario Festas', '2025-01-25', 'Kit Safari', 150, 'entregue',"
                " '2026-09-19T10:00:00')", (cli,)).lastrowid
        r = admin.get("/pedidos")
        assert f"/pedido/historico/{hid}" in r.text and "Formulario Festas" in r.text
        d = admin.get(f"/pedido/historico/{hid}")
        assert "Pedido importado #351" in d.text and "Registro importado" in d.text
        assert "150,00" in d.text and "Marcar como" not in d.text
        assert admin.get("/pedido/historico/9999").status_code == 404

    def test_morumbi_3d_continua_oculto(self, app, admin):
        cli = _cliente()
        with dados.conectar() as conn:
            hid = conn.execute(
                "INSERT INTO eventos_historico (cliente_id, origem_id, origem, data_evento,"
                " valor, status_origem) VALUES (?, 9, 'Morumbi 3D', '2025-01-25', 15,"
                " 'entregue')", (cli,)).lastrowid
        assert dados.consultar_pedidos()["total"] == 0
        assert admin.get(f"/pedido/historico/{hid}").status_code == 404


# --- Cliente, detalhe e agenda ---------------------------------------------

class TesteDetalhe:
    def test_cabecalho_cards_e_cliente(self, app, admin):
        cli = _cliente()
        pid = _pedido(cli, servicos=["Entrega"], local_evento="Buffet Alegria",
                      hora_retirada="14:00", forma_pagamento="Pix",
                      condicao_pagamento="50% na reserva", responsavel="Equipe Morumbi")
        r = admin.get(f"/pedido/{pid}")
        for texto in (f"Pedido #{pid}", "Juliana Mello • Festa em 10/10/2026", "Confirmado",
                      "Em preparação", "Morumbi Festas", "Informações do pedido",
                      "Itens contratados", "Linha do tempo", "Logística", "Comercial",
                      "Próxima ação", "Marcar como separado", "Buffet Alegria",
                      "10/10/2026 • 14:00", "Pix • 50% na reserva", "Equipe Morumbi",
                      "R$\xa0400,00", "0 festas realizadas", "Ver cliente"):
            assert texto in r.text, texto
        assert f"/cliente/{cli}" in r.text

    def test_linha_do_tempo_so_com_fatos(self, app):
        pid = _pedido(_cliente(), evento="2026-10-10", retirada="2026-10-09",
                      devolucao="2026-10-11", hora_retirada="14:00")
        linha = dados.buscar_pedido_detalhe(pid)["linha_do_tempo"]
        titulos = [e["titulo"] for e in linha]
        assert titulos == ["Pedido criado", "Retirada / entrega", "Festa", "Devolução"]
        assert linha[1]["quando"] == "2026-10-09T14:00:00"
        assert all(e["categoria"] == "Agenda" for e in linha[1:])

    def test_ver_na_agenda_destaca_o_pedido(self, app, admin):
        pid = _pedido(_cliente(), evento="2026-10-10")
        r = admin.get(f"/pedido/{pid}")
        assert f"destaque={pid}" in r.text and "visao=diaria" in r.text
        agenda = admin.get(f"/agenda?visao=diaria&ano=2026&mes=10&dia=10&destaque={pid}")
        assert f'class="ag-destaque" id="pedido-{pid}"' in agenda.text

    def test_pedido_vinculado_ao_cliente_correto(self, app):
        a, b = _cliente("Ana"), _cliente("Bia")
        pid = _pedido(a)
        assert [p["id"] for p in dados.pedidos_cliente(a)] == [pid]
        assert dados.pedidos_cliente(b) == []


# --- Itens, datas, valores, concorrência e auditoria ------------------------

class TesteEdicao:
    def _form(self, cli, **extra):
        f = {"cliente_id": str(cli), "data_evento": "2026-10-10",
             "data_retirada": "2026-10-09", "data_devolucao": "2026-10-11",
             "item_tipo_0": "kit", "item_descricao_0": "Kit Safari",
             "item_quantidade_0": "1", "item_preco_0": "320",
             "item_tipo_3": "produto", "item_descricao_3": "Bandeja",
             "item_quantidade_3": "4", "item_preco_3": "20"}
        f.update(extra)
        return f

    def test_itens_com_lacuna_de_indice_nao_se_perdem(self, app, admin):
        cli = _cliente()
        admin.post("/pedido/novo", data=self._form(cli))
        ped = dados.listar_pedidos()[0]
        completo = dados.buscar_pedido_festas(ped["id"])
        assert [i["descricao"] for i in completo["itens"]] == ["Kit Safari", "Bandeja"]
        assert completo["total"] == 400.0

    def test_inclusao_alteracao_exclusao_auditadas(self, app, admin):
        cli = _cliente()
        pid = _pedido(cli)
        versao = dados.buscar_pedido_festas(pid)["atualizado_em"]
        admin.post(f"/pedido/{pid}/editar", data={
            "cliente_id": str(cli), "data_evento": "2026-10-10", "versao": versao,
            "status_comercial": "confirmado", "status_operacional": "preparacao",
            "item_tipo_0": "kit", "item_descricao_0": "Kit Safari",
            "item_quantidade_0": "2", "item_preco_0": "300",
            "item_tipo_5": "servico", "item_descricao_5": "Montagem",
            "item_quantidade_5": "1", "item_preco_5": "50"})
        ped = dados.buscar_pedido_festas(pid)
        assert [(i["descricao"], i["quantidade"]) for i in ped["itens"]] == [
            ("Kit Safari", 2), ("Montagem", 1)]
        assert ped["total"] == 650.0
        ev = _eventos(pid)[-1]
        assert ev["titulo"] == "Pedido editado" and "itens" in ev["mudancas"]
        assert ev["usuario_id"] == 1

    @pytest.mark.parametrize("extra,mensagem", [
        ({"item_quantidade_0": "0"}, "deve ser ao menos 1"),
        ({"item_preco_0": "-5"}, "não pode ser negativo"),
        ({"item_tipo_0": "brinde"}, "Tipo de item inválido"),
        ({"data_evento": "2026-02-31"}, "Data inválida"),
        ({"hora_retirada": "25:00"}, "Horário inválido"),
        ({"data_retirada": "2026-10-12"}, "posterior à retirada"),
        ({"cliente_id": "99999"}, "Cliente não encontrado"),
    ])
    def test_validacoes_no_servidor(self, app, admin, extra, mensagem):
        cli = _cliente()
        r = admin.post("/pedido/novo", data=self._form(cli, **extra), follow_redirects=True)
        assert mensagem in r.text
        assert dados.listar_pedidos() == []

    def test_conflito_de_edicao_simultanea(self, app, admin):
        cli = _cliente()
        pid = _pedido(cli)
        versao_antiga = dados.buscar_pedido_festas(pid)["atualizado_em"]
        with dados.conectar() as conn:
            conn.execute("UPDATE pedidos SET atualizado_em = '2099-01-01T00:00:00',"
                         " observacoes = 'outra pessoa' WHERE id = ?", (pid,))
        r = admin.post(f"/pedido/{pid}/editar",
                       data=self._form(cli, versao=versao_antiga, observacoes="minha"),
                       follow_redirects=True)
        assert "alterado por outra pessoa" in r.text
        assert dados.buscar_pedido_festas(pid)["observacoes"] == "outra pessoa"

    def test_pedido_sem_valor(self, app, admin):
        pid = _pedido(_cliente(), itens=[{"tipo": "servico", "descricao": "Entrega",
                                          "quantidade": 1, "preco_unitario": 0}])
        registro = dados.consultar_pedidos()["registros"][0]
        assert registro["total"] == 0 and not registro["itens"]
        linha = re.search(rf'#{pid}</a>.*?</tr>', admin.get("/pedidos").text, re.S).group(0)
        assert 'data-rotulo="Total">—</td>' in linha
