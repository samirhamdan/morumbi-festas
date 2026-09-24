"""Testes da Agenda (Sprint 08 e Sprint 4)."""

from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from sistema import dados, formato


def _cliente(admin, nome="Ana Souza"):
    admin.post("/cliente", data={"nome": nome, "whatsapp": ""},
               follow_redirects=True)
    return [c for c in dados.listar_clientes() if c["nome"] == nome][0]


def _pedido(admin, cliente_id, data_evento="2026-10-15",
            data_ret="2026-10-14", data_dev="2026-10-16", **extra):
    form = {
        "cliente_id": str(cliente_id),
        "data_evento": data_evento,
        "data_retirada": data_ret,
        "data_devolucao": data_dev,
        "status_comercial": extra.get("status_comercial", "confirmado"),
        "status_operacional": extra.get("status_operacional", "preparacao"),
        "observacoes": "",
        "item_tipo_0": "produto",
        "item_item_id_0": "",
        "item_descricao_0": "Item teste",
        "item_quantidade_0": "1",
        "item_preco_0": "100.00",
    }
    return admin.post("/pedido/novo", data=form, follow_redirects=True)


# --- dados.eventos_agenda ---

class TesteEventosAgenda:
    def test_retorna_pedidos_no_periodo(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31")
        assert len(evts) == 1
        assert evts[0]["cliente_nome"] == cli["nome"]

    def test_fora_do_periodo_nao_retorna(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-11-01", "2026-11-30")
        assert len(evts) == 0

    def test_filtro_tipo_evento(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15",
                data_ret="2026-10-14", data_dev="2026-10-16")
        evts = dados.eventos_agenda("2026-10-15", "2026-10-15", tipo="evento")
        assert len(evts) == 1
        evts = dados.eventos_agenda("2026-10-14", "2026-10-14", tipo="evento")
        assert len(evts) == 0

    def test_filtro_tipo_retirada(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-10-14", "2026-10-14", tipo="retirada")
        assert len(evts) == 1
        evts = dados.eventos_agenda("2026-10-15", "2026-10-15", tipo="retirada")
        assert len(evts) == 0

    def test_filtro_tipo_devolucao(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        evts = dados.eventos_agenda("2026-10-16", "2026-10-16", tipo="devolucao")
        assert len(evts) == 1

    def test_filtro_status_comercial(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], status_comercial="confirmado")
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31",
                                    status_comercial="confirmado")
        assert len(evts) == 1
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31",
                                    status_comercial="entregue")
        assert len(evts) == 0

    def test_cancelado_nao_aparece(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        peds = dados.listar_pedidos()
        dados.cancelar_pedido(peds[0]["id"])
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31")
        assert len(evts) == 0

    def test_multiplos_pedidos(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-10",
                data_ret="2026-10-09", data_dev="2026-10-11")
        _pedido(admin, cli["id"], data_evento="2026-10-20",
                data_ret="2026-10-19", data_dev="2026-10-21")
        evts = dados.eventos_agenda("2026-10-01", "2026-10-31")
        assert len(evts) == 2


# --- rotas agenda ---

class TesteRotaAgenda:
    def test_agenda_mensal_200(self, app, admin):
        r = admin.get("/agenda")
        assert r.status_code == 200
        assert "Agenda" in r.text

    def test_agenda_mensal_com_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15")
        r = admin.get("/agenda?visao=mensal&ano=2026&mes=10")
        assert r.status_code == 200
        assert cli["nome"] in r.text or "1" in r.text

    def test_agenda_semanal_200(self, app, admin):
        r = admin.get("/agenda?visao=semanal&ano=2026&mes=10&dia=15")
        assert r.status_code == 200
        assert "Seg" in r.text

    def test_agenda_semanal_com_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15",
                data_ret="2026-10-14", data_dev="2026-10-16")
        r = admin.get("/agenda?visao=semanal&ano=2026&mes=10&dia=14")
        assert r.status_code == 200
        assert cli["nome"] in r.text

    def test_agenda_diaria_200(self, app, admin):
        r = admin.get("/agenda?visao=diaria&ano=2026&mes=10&dia=15")
        assert r.status_code == 200
        assert "Retiradas" in r.text
        assert "Devoluções" in r.text
        assert "Eventos" in r.text

    def test_agenda_diaria_com_pedido(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"], data_evento="2026-10-15",
                data_ret="2026-10-14", data_dev="2026-10-16")
        r = admin.get("/agenda?visao=diaria&ano=2026&mes=10&dia=14")
        assert r.status_code == 200
        assert cli["nome"] in r.text

    def test_agenda_filtro_tipo(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        r = admin.get("/agenda?visao=mensal&ano=2026&mes=10&tipo=retirada")
        assert r.status_code == 200

    def test_agenda_filtro_status(self, app, admin):
        cli = _cliente(admin)
        _pedido(admin, cli["id"])
        r = admin.get("/agenda?visao=mensal&ano=2026&mes=10&status=confirmado")
        assert r.status_code == 200

    def test_agenda_sem_login_redireciona(self, app, client):
        r = client.get("/agenda")
        assert r.status_code == 302

    def test_agenda_menu_operacao(self, app, admin):
        r = admin.get("/agenda")
        assert "Operação" in r.text



# --- Sprint 4: Agenda como projeção dos pedidos -----------------------------

HOJE = date.fromisoformat(formato.agora()[:10])
D = lambda n: (HOJE + timedelta(days=n)).isoformat()  # noqa: E731
SET = ("2026-09-01", "2026-09-30")


def _cli(nome="Luciana Almeida", whatsapp=""):
    return dados.salvar_cliente({"nome": nome, "whatsapp": whatsapp})


def _ped(cli, evento="2026-09-24", retirada="2026-09-24", devolucao="2026-09-26",
         servicos=("Entrega",), itens=None, **extra):
    d = {"cliente_id": cli, "data_evento": evento, "data_retirada": retirada,
         "data_devolucao": devolucao}
    d.update(extra)
    lista = itens if itens is not None else [
        {"tipo": "kit", "descricao": "Kit Safari", "quantidade": 1, "preco_unitario": 300},
        {"tipo": "produto", "descricao": "Bandejas", "quantidade": 11, "preco_unitario": 10}]
    lista = lista + [{"tipo": "servico", "descricao": s, "quantidade": 1, "preco_unitario": 0}
                     for s in servicos]
    return dados.salvar_pedido_festas(d, lista)


def _comp(inicio=SET[0], fim=SET[1], agora="2026-09-01T00:00:00", **filtros):
    return dados.agenda_periodo(inicio, fim, filtros, agora_=agora)


def _chaves(**kw):
    return [(c["pedido_id"] or c["historico_id"], c["tipo"], c["data"], c["hora"])
            for c in _comp(**kw)["compromissos"]]


def _sql(sql, *params):
    with dados.conectar() as conn:
        return conn.execute(sql, params).lastrowid


def _usuario(nome, login):
    return dados.salvar_usuario({"nome": nome, "login": login, "perfil": "operacional"},
                                senha_hash=generate_password_hash("x"))


class TesteProjecao:
    def test_pedido_ativo_gera_evento_saida_e_devolucao(self, app):
        pid = _ped(_cli(), hora_retirada="08:00", hora_devolucao="09:00")
        assert _chaves() == [(pid, "evento", "2026-09-24", ""),
                             (pid, "entrega", "2026-09-24", "08:00"),
                             (pid, "devolucao", "2026-09-26", "09:00")]
        c = _comp()["compromissos"][1]
        assert (c["cliente_nome"], c["principal"], c["itens"]) == ("Luciana Almeida", "Kit Safari", 12)
        assert c["rotulo_status"] == "Em preparação" and c["operacional"]

    def test_sem_servico_de_entrega_e_retirada_pelo_cliente(self, app):
        pid = _ped(_cli(), servicos=("Retirada", "Devolução"), hora_retirada="14:00")
        assert (pid, "retirada", "2026-09-24", "14:00") in _chaves()
        assert _ped(_cli("Ana"), servicos=("Montagem",)) in [
            k[0] for k in _chaves(tipo="entrega")]

    def test_nada_e_gravado_e_so_o_periodo_e_lido(self, app):
        _ped(_cli())
        antes = {t: _sql(f"SELECT COUNT(*) FROM {t}") for t in ("pedidos", "audit_log")}
        with dados.conectar() as conn:
            antes = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ("pedidos", "itens_pedido", "audit_log", "eventos_historico")}
        assert _comp("2026-10-01", "2026-10-31")["compromissos"] == []
        assert len(_comp()["compromissos"]) == 3
        with dados.conectar() as conn:
            depois = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in antes}
            indices = {r[1] for r in conn.execute("PRAGMA index_list(pedidos)")}
        assert antes == depois
        assert {"ix_pedidos_tenant_evento", "ix_pedidos_tenant_retirada",
                "ix_pedidos_tenant_devolucao"} <= indices

    def test_alteracao_de_data_atualiza_a_agenda(self, app):
        cli = _cli()
        pid = _ped(cli)
        p = dados.buscar_pedido_festas(pid)
        dados.salvar_pedido_festas(
            {"cliente_id": cli, "data_evento": "2026-09-28", "data_retirada": "2026-09-27",
             "data_devolucao": "2026-09-29", "hora_retirada": "10:30",
             "versao": p["atualizado_em"]}, p["itens"], pid)
        assert [(k[1], k[2], k[3]) for k in _chaves()] == [
            ("entrega", "2026-09-27", "10:30"), ("evento", "2026-09-28", ""),
            ("devolucao", "2026-09-29", "")]
        tempo = dados.buscar_pedido_detalhe(pid)["linha_do_tempo"]
        assert any(e["titulo"] == "Pedido editado" for e in tempo)  # auditoria da mudança

    def test_esteira_reflete_na_agenda(self, app):
        pid = _ped(_cli(), hora_retirada="08:00")
        agora = "2026-09-24T09:00:00"
        saida = [c for c in _comp(agora=agora)["compromissos"] if c["tipo"] == "entrega"][0]
        assert saida["atrasado"] and saida["etapa"] == "preparacao"
        dados.mover_etapa(pid, "separado")
        dados.mover_etapa(pid, "entregue")
        saida = [c for c in _comp(agora=agora)["compromissos"] if c["tipo"] == "entrega"][0]
        assert not saida["atrasado"] and saida["rotulo_status"] == "Entregue/Retirado"
        assert [k[0] for k in _chaves(status="entregue")] == [pid] * 3


class TesteHistoricoECancelado:
    def test_pedido_historico_aparece_so_para_consulta(self, app):
        pid = _ped(_cli(), evento="2026-09-10", retirada="2026-09-09", devolucao="2026-09-11")
        _sql("UPDATE pedidos SET historico = 1, status_comercial = 'finalizado',"
             " status_operacional = 'finalizado' WHERE id = ?", pid)
        r = _comp(agora="2026-09-30T00:00:00")
        assert [(c["tipo"], c["data"]) for c in r["compromissos"]] == [("historico", "2026-09-10")]
        c = r["compromissos"][0]
        assert not c["operacional"] and not c["atrasado"] and c["alertas"] == []
        assert r["kpis"]["historico"] == 1 and r["kpis"]["eventos"] == 1
        assert r["kpis"]["retirada"] == r["kpis"]["entrega"] == r["kpis"]["devolucao"] == 0
        assert r["kpis"]["atrasados"] == 0
        assert pid not in [p["id"] for e in dados.esteira_pedidos() for p in e["pedidos"]]

    def test_importado_da_planilha(self, app, admin):
        cli = _cli("Maria Silva")
        dados.salvar_evento_historico({"cliente_id": cli, "origem_id": 777,
                                       "data_evento": "2026-09-05", "descricao": "Festa Junina",
                                       "status_origem": "entregue", "origem": "Formulario Festas"})
        c = _comp()["compromissos"][0]
        assert (c["tipo"], c["rotulo_status"], c["pedido_id"]) == ("historico", "Histórico importado", None)
        r = admin.get("/agenda?visao=diaria&data=2026-09-05")
        assert "Histórico importado" in r.text and "Maria Silva" in r.text
        assert "Ver na esteira" not in r.text and "Registrar ocorrência" not in r.text

    def test_cancelado_fora_da_operacao_e_dos_numeros(self, app):
        pid = _ped(_cli())
        dados.cancelar_pedido(pid, "desistiu")
        r = _comp()
        assert r["compromissos"] == [] and r["kpis"]["eventos"] == 0 and r["total_periodo"] == 0
        cancelados = _comp(status="cancelado")["compromissos"]
        assert {c["pedido_id"] for c in cancelados} == {pid}
        assert all(c["cancelado"] and not c["operacional"] and not c["atrasado"] for c in cancelados)


class TesteAtrasoEConflito:
    def test_atraso_usa_status_real_e_horario(self, app):
        pid = _ped(_cli(), hora_retirada="08:00", hora_devolucao="10:00",
                   evento="2026-09-24", retirada="2026-09-24", devolucao="2026-09-25")
        def atrasos(agora):
            return {c["tipo"] for c in _comp(agora=agora)["compromissos"] if c["atrasado"]}
        assert atrasos("2026-09-24T07:59:00") == set()
        assert atrasos("2026-09-24T08:01:00") == {"entrega"}   # passou das 08:00 sem sair
        dados.mover_etapa(pid, "separado")
        dados.mover_etapa(pid, "entregue")
        assert atrasos("2026-09-24T08:01:00") == set()
        assert atrasos("2026-09-25T10:01:00") == {"devolucao"}
        dados.mover_etapa(pid, "recolhido")
        assert atrasos("2026-09-26T00:00:00") == set()
        assert _comp(agora="2026-09-25T10:01:00")["kpis"]["atrasados"] == 0

    def test_evento_so_conta_prazo_sem_data_de_saida(self, app):
        _ped(_cli(), retirada=None, devolucao=None)
        c = _comp(agora="2026-09-25T00:00:00")["compromissos"][0]
        assert c["tipo"] == "evento" and c["atrasado"]
        motivos = [m for _, m in c["alertas"]]
        assert "Pedido sem data de retirada/entrega." in motivos

    def test_datas_incoerentes_de_dado_antigo(self, app):
        pid = _ped(_cli())
        _sql("UPDATE pedidos SET data_devolucao = '2026-09-20', data_retirada = '2026-09-19'"
             " WHERE id = ?", pid)
        r = _comp()
        motivos = {m for c in r["compromissos"] for n, m in c["alertas"] if n == "conflito"}
        assert "Devolução marcada antes do evento." in motivos
        assert r["kpis"]["pedidos_com_conflito"] == 1
        assert {c["pedido_id"] for c in _comp(status="conflito")["compromissos"]} == {pid}

    def test_mesmo_responsavel_em_horarios_proximos(self, app):
        joao = _usuario("João Silva", "joao")
        a = _ped(_cli(), hora_retirada="08:00", responsavel_id=str(joao))
        b = _ped(_cli("Marcos Pereira"), hora_retirada="08:20", responsavel_id=str(joao))
        c = _ped(_cli("Ana"), hora_retirada="09:00", responsavel_id=str(joao))
        alertas = {x["pedido_id"]: [m for _, m in x["alertas"]]
                   for x in _comp()["compromissos"] if x["tipo"] == "entrega"}
        assert alertas[a] == [f"João Silva também tem entrega do pedido #{b} às 08:20."]
        assert alertas[b] == [f"João Silva também tem entrega do pedido #{a} às 08:00."]
        assert alertas[c] == []

    def test_estoque_insuficiente_de_dado_antigo(self, app):
        prod = dados.salvar_produto({"nome": "Mesa", "status": "disponivel", "quantidade_total": 5})
        itens = [{"tipo": "produto", "item_id": prod, "descricao": "Mesa", "quantidade": 4,
                  "preco_unitario": 10}]
        a = _ped(_cli(), itens=itens, servicos=())
        b = _ped(_cli("Ana"), evento="2026-09-10", retirada="2026-09-10",
                 devolucao="2026-09-11", itens=itens, servicos=())
        _sql("UPDATE pedidos SET data_evento='2026-09-24', data_retirada='2026-09-24',"
             " data_devolucao='2026-09-26' WHERE id = ?", b)  # sobreposição antiga
        motivos = {c["pedido_id"]: {m for _, m in c["alertas"]} for c in _comp()["compromissos"]}
        assert any("Quantidade insuficiente para 'Mesa'" in m for m in motivos[a])
        assert any("Disponível: 1" in m for m in motivos[b])

    def test_pedido_sem_nenhuma_data_e_avisado(self, app, admin):
        pid = _ped(_cli(), evento=None, retirada=None, devolucao=None)
        assert [p["id"] for p in _comp()["sem_data"]] == [pid]
        assert f'href="/pedido/{pid}"' in admin.get("/agenda?data=2026-09-10").text


class TesteValidacaoAoSalvar:
    def test_datas_precisam_combinar_com_o_evento(self, app):
        cli = _cli()
        for extra, campo in (({"retirada": "2026-09-25"}, "data_retirada"),
                             ({"devolucao": "2026-09-23", "retirada": "2026-09-22"}, "data_devolucao"),
                             ({"retirada": "2026-09-24", "devolucao": "2026-09-24",
                               "hora_retirada": "10:00", "hora_devolucao": "09:00"}, "hora_devolucao")):
            try:
                _ped(cli, **extra)
                assert False, extra
            except dados.ErroDeCampo as e:
                assert e.campo == campo


class TesteFiltrosEBusca:
    def test_filtros_combinados(self, app):
        joao, carlos = _usuario("João Silva", "joao"), _usuario("Carlos Mendes", "carlos")
        a = _ped(_cli(), responsavel_id=str(joao))
        b = _ped(_cli("Marcos Pereira"), servicos=("Retirada",), responsavel_id=str(carlos))
        c = _ped(_cli("Empresa ABC"), evento="2026-09-12", retirada="2026-09-12",
                 devolucao="2026-09-13", responsavel_id=str(joao))
        assert {k[0] for k in _chaves(tipo="entrega")} == {a, c}
        assert {k[0] for k in _chaves(tipo="entrega", responsavel="joão silva")} == {a, c}
        assert {k[0] for k in _chaves(tipo="entrega", responsavel="João Silva",
                                      q="empresa")} == {c}
        assert {k[0] for k in _chaves(servico="Retirada")} == {b}
        assert {k[0] for k in _chaves(servico="entrega", tipo="devolucao")} == {a, c}
        assert _chaves(tipo="historico") == []
        assert _chaves(servico="Retirada", responsavel="João Silva") == []

    def test_busca_por_pedido_cliente_kit_e_telefone(self, app):
        a = _ped(_cli())
        b = _ped(_cli("Marcos Pereira", "67 98888-0000"), itens=[
            {"tipo": "kit", "descricao": "Kit Princesas", "quantidade": 1, "preco_unitario": 280}])
        assert {k[0] for k in _chaves(q=f"#{b}")} == {b}
        assert {k[0] for k in _chaves(q=str(a))} == {a}
        assert {k[0] for k in _chaves(q="luciana")} == {a}
        assert {k[0] for k in _chaves(q="princesas")} == {b}
        assert {k[0] for k in _chaves(q="8888")} == {b}

    def test_valores_invalidos_sao_ignorados(self, app, admin):
        _ped(_cli())
        f = dados.filtros_agenda({"tipo": "x", "status": "y", "q": " luciana "})
        assert f == {"tipo": "", "servico": "", "responsavel": "", "status": "", "q": "luciana"}
        for url in ("/agenda?data=invalida", "/agenda?visao=anual&tipo=x&status=y",
                    "/agenda?ano=2026&mes=13", "/agenda?visao=diaria&data=2026-02-30",
                    "/agenda?data=9999-12-31", "/agenda?visao=semanal&data=0001-01-01"):
            assert admin.get(url).status_code == 200, url
        try:
            dados.agenda_periodo("2026-09-30", "2026-09-01")
            assert False
        except ValueError:
            pass


class TesteTela:
    def test_mensal_com_figma(self, app, admin):
        pid = _ped(_cli(), hora_retirada="08:00")
        r = admin.get("/agenda?data=2026-09-24")
        t = r.text
        for texto in ("Agenda", "Visualize todos os eventos, retiradas, entregas e devoluções",
                      "Novo pedido", "Mensal", "Semanal", "Diária", "Setembro 2026", "Hoje",
                      "Eventos no mês", "Retiradas", "Entregas", "Devoluções", "Em atraso",
                      "Todos os eventos", "Todos os serviços", "Todos os responsáveis",
                      "Limpar filtros", "Qui, 24 de Setembro", "Evento (Festa)", "Histórico",
                      "Eventos históricos ficam visíveis para consulta"):
            assert texto in t, texto
        assert f'data-chip="p{pid}-entrega"' in t and "08:00</span>" in t
        assert 'agd-cel agd-cel--sel' in t or 'agd-cel--sel' in t
        assert f'href="/pedido/{pid}"' in t and "Ver na esteira" in t and "Ver cliente" in t
        assert f"#ocorrencia" in t
        assert 'class="agd-fechar" data-fechar' in t  # painel vira gaveta no celular

    def test_mais_de_tres_no_dia_vira_mais(self, app, admin):
        for i in range(4):
            _ped(_cli(f"Cliente {i}"), servicos=())
        t = admin.get("/agenda?data=2026-09-24").text
        assert "+ 1 mais" in t and "+ 2 mais" not in t  # dia 24: 4 eventos + 4 retiradas = 8
        assert "visao=diaria" in t

    def test_semanal_e_diaria(self, app, admin):
        pid = _ped(_cli(), hora_retirada="08:00")
        s = admin.get("/agenda?visao=semanal&data=2026-09-24").text
        assert "20 – 26 set 2026" in s and "Seg" in s and "Luciana Almeida" in s
        d = admin.get(f"/agenda?visao=diaria&data=2026-09-24&destaque={pid}").text
        assert "Qui, 24 de Setembro de 2026" in d and "Festas do dia" in d and "08:00" in d
        assert f'id="pedido-{pid}"' in d and "agd-card--destaque" in d
        assert "Eventos no dia" in d

    def test_navegacao_e_links_antigos(self, app, admin):
        t = admin.get("/agenda?data=2026-01-31").text
        assert "data=2025-12-31" in t and "data=2026-02-28" in t
        t = admin.get("/agenda?visao=semanal&data=2026-09-24").text
        assert "data=2026-09-17" in t and "data=2026-10-01" in t
        assert "Outubro 2026" in admin.get("/agenda?ano=2026&mes=10").text
        assert "Seg, 12 de Outubro de 2026" in admin.get("/agenda?visao=diaria&ano=2026&mes=10&dia=12").text

    def test_estados_vazios(self, app, admin):
        assert "Nenhum compromisso neste período." in admin.get("/agenda?data=2030-01-10").text
        _ped(_cli())
        t = admin.get("/agenda?data=2026-09-10&q=zzz").text
        assert "Nenhum evento encontrado para os filtros selecionados." in t
        assert "Nenhum compromisso para este dia." in admin.get("/agenda?data=2026-09-10").text

    def test_parcial(self, app, admin):
        _ped(_cli())
        r = admin.get("/agenda?data=2026-09-24&parcial=1")
        assert "<html" not in r.text
        for parte in ("topo", "kpis", "cal", "painel"):
            assert f'data-parte="{parte}"' in r.text
        assert 'data-ref="2026-09-24"' in r.text

    def test_dashboard_abre_a_agenda_no_pedido(self, app, admin):
        pid = _ped(_cli(), evento=D(0), retirada=D(0), devolucao=D(1))
        t = admin.get("/").text
        assert f"destaque={pid}" in t and f"#pedido-{pid}" in t


class TestePermissaoEEmpresa:
    def _entrar(self, app, perfil):
        dados.salvar_usuario({"nome": perfil.title(), "login": f"a_{perfil}", "perfil": perfil},
                             senha_hash=generate_password_hash("x"))
        c = app.test_client()
        c.post("/entrar", data={"login": f"a_{perfil}", "senha": "x"})
        return c

    def test_acoes_conforme_perfil(self, app, admin):
        _ped(_cli())
        comercial = self._entrar(app, "comercial").get("/agenda?data=2026-09-24").text
        assert "Ver pedido" in comercial and "Ver na esteira" not in comercial
        assert "Novo pedido" in comercial
        visual = self._entrar(app, "visualizacao").get("/agenda?data=2026-09-24").text
        assert "Novo pedido" not in visual and "Registrar ocorrência" not in visual

    def test_outra_empresa_nao_aparece(self, app):
        _ped(_cli())
        b = dados.criar_empresa("Empresa Teste")
        with dados.usando_tenant(b):
            assert _comp()["compromissos"] == []
            outro = dados.salvar_cliente({"nome": "Cliente B"})
            _ped(outro)
            assert {c["cliente_nome"] for c in _comp()["compromissos"]} == {"Cliente B"}
        assert {c["cliente_nome"] for c in _comp()["compromissos"]} == {"Luciana Almeida"}
