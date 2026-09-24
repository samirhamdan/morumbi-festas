#!/usr/bin/env python3
"""Sprint 2.1 — operação Morumbi Festas e classificação pelo estado real.

Importado não é histórico: festas da planilha "Formulario Festas" que ainda
vão acontecer viram pedidos atuais da Morumbi Festas (Em andamento, Agenda e,
no Sprint 3, Esteira). O registro importado continua intacto e ligado ao
pedido. Nada é apagado, nenhum cliente é criado ou mesclado, nenhum item ou
valor é inventado.

1) Diagnóstico + simulação (padrão). O banco real só é lido: tudo roda numa
   cópia temporária, que é apagada no fim.

    FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \\
        venv/bin/python ferramentas/reclassificar_pedidos.py

2) Aplicação no banco real, somente depois de validar o relatório acima.
   Faz backup verificado antes de alterar qualquer registro.

    FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \\
        venv/bin/python ferramentas/reclassificar_pedidos.py --aplicar

A rotina é idempotente: executar de novo não altera nada.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sistema import dados, formato  # noqa: E402
from ferramentas.normalizar_historico import (  # noqa: E402
    copiar_banco, secao, titulo, usar_banco)


def data(v) -> str:
    return formato.fmt_data(v) if v else "sem data"


def contagens(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("pedidos", "itens_pedido", "clientes", "eventos_historico")}


def imprimir_estrutura(conn):
    titulo("ETAPA 1 — ESTRUTURA E CAMPOS DE ORIGEM")
    for tabela in ("pedidos", "eventos_historico", "clientes"):
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({tabela})")]
        n = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
        print(f"\n{tabela} ({n} registros)\n  campos: {', '.join(cols)}")
    print("\nOnde está cada conceito:")
    print("  operação ....... fixa: Morumbi Festas (Morumbi 3D segue oculto)")
    print("  fonte técnica .. eventos_historico.origem / origem_id (preservados);"
          " pedidos.fonte / fonte_id nos convertidos")
    print("  canal .......... eventos_historico.canal (texto original);"
          " pedidos.canal (padronizado)")
    print("  canal do cliente clientes.origem (cadastro do cliente, não alterado)")

    secao("eventos_historico — fonte × status na origem")
    for r in conn.execute(
            "SELECT COALESCE(origem, '(sem)'), COALESCE(status_origem, ''),"
            " COUNT(*) FROM eventos_historico GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"  {r[0]:<20} {r[1]:<12} {r[2]}")


def imprimir_resumo(diag: dict, rotulo: str):
    r = diag["resumo"]
    titulo(f"RELATÓRIO {rotulo} (hoje: {data(diag['hoje'])},"
           f" corte do Sprint 1: {data(diag['data_corte'])})")
    linhas = (
        ("1. Pedidos analisados (sistema + importados visíveis)", r["analisados"]),
        ("2. Históricos (encerrados)", r["historicos"]),
        ("3. Futuros (evento de hoje em diante, sem cancelados)", r["futuros"]),
        ("4a. A reclassificar como atuais", r["a_reclassificar"]),
        ("4b. Já reclassificados como atuais", r["reclassificados"]),
        ("5. Cancelados", r["cancelados"]),
        ("6. Finalizados", r["finalizados"]),
        ("7. Com fonte Formulário Festas", r["formulario_festas"]),
        ("8. Com canal identificado", r["canal_identificado"]),
        (f"9. Possíveis clientes duplicados ({r['grupos_duplicados']} grupos)",
         r["clientes_duplicados"]),
        ("10. Registros que exigem análise manual", r["analise_manual"]),
    )
    for texto, n in linhas:
        print(f"  {texto:<58} {n}")


def imprimir_detalhes(diag: dict, amostra: int):
    p, imp = diag["pedidos"], diag["importados"]
    secao("Pedidos do sistema")
    for chave, texto in (("total", "total"), ("em_andamento", "em andamento"),
                         ("finalizados", "finalizados"), ("cancelados", "cancelados"),
                         ("historicos", "marcados como histórico"),
                         ("futuros", "com evento futuro"),
                         ("passados_com_operacao_pendente",
                          "evento passado, operação pendente (continuam em andamento)"),
                         ("sem_itens_em_andamento", "em andamento sem itens"),
                         ("promovidos", "criados a partir de importados")):
        print(f"  {texto:<62} {p[chave]}")
    if p["historico_inconsistente"]:
        print(f"\n  Marcados como histórico sem estar encerrados"
              f" (a marca será retirada): {len(p['historico_inconsistente'])}")
        for x in p["historico_inconsistente"][:amostra]:
            print(f"    #{x['id']}  {data(x['data_ref'])}  {x['status_comercial']}")

    secao("Importados visíveis (Formulário Festas)")
    for s, n in imp["por_situacao"].items():
        print(f"  {s:<12} {n}")
    print(f"  ocultos (Morumbi 3D): {imp['ocultos']}")

    secao(f"A reclassificar como pedido atual: {len(imp['a_promover'])}")
    for h in imp["a_promover"][:amostra]:
        print(f"  planilha #{h['origem_id']:<6} {data(h['data_evento'])}"
              f"  {h['cliente_nome'] or '—'}  canal: {h['canal'] or '—'}"
              f"  valor: {formato.dinheiro(h['valor']) if h['valor'] else 'não informado'}")
    if len(imp["a_promover"]) > amostra:
        print(f"  ... e mais {len(imp['a_promover']) - amostra}")

    secao("Canais (padronizados)")
    for c, n in sorted(diag["canais"].items(), key=lambda x: -x[1]):
        print(f"  {c:<18} {n}")

    secao("Análise manual (nada disto é alterado pela rotina)")
    for h in imp["sem_evidencia"]:
        print(f"  importado #{h['origem_id']}  {data(h['data_evento'])}"
              f"  {h['cliente_nome'] or '—'}: {', '.join(h['motivos'])}")
    for h in imp["finalizados_data_futura"]:
        print(f"  importado #{h['origem_id']}  {data(h['data_evento'])}"
              f"  {h['cliente_nome'] or '—'}: entregue na planilha com data futura"
              " (corrigir a data em Relatórios)")
    for h in diag["vinculos_ambiguos"]:
        print(f"  importado #{h['origem_id']}  {data(h['data_evento'])}"
              f"  {h['cliente_nome']}: há outro cliente com o mesmo nome"
              " (conferir se o vínculo está certo)")
    for x in diag["finalizados_com_operacao_em_curso"]:
        print(f"  pedido #{x['id']}  {data(x['data_evento'])}  {x['cliente_nome'] or '—'}:"
              f" finalizado pelo corte do Sprint 1 quando estava '{x['status_antes']}'"
              " (devolução/conferência pode estar pendente)")
    if not diag["resumo"]["analise_manual"]:
        print("  nenhum")

    secao(f"Possíveis clientes duplicados: {len(diag['clientes_duplicados'])} grupos"
          " (só relatório, nada é mesclado)")
    for g in diag["clientes_duplicados"][:amostra]:
        nomes = "; ".join(f"#{c['id']} {c['nome']}" for c in g["clientes"])
        print(f"  {nomes}  — {', '.join(g['motivos'])}")
    if len(diag["clientes_duplicados"]) > amostra:
        print(f"  ... e mais {len(diag['clientes_duplicados']) - amostra} grupos")


def conferir(conn) -> list:
    """Regras que precisam valer depois da execução."""
    problemas = []
    um = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    v = dados.verificar_operacao_sem_historicos(conn)
    for chave, n in v.items():
        if n:
            problemas.append(f"histórico em operação ({chave}): {n}")
    n = um("SELECT COUNT(*) FROM (SELECT historico_id FROM pedidos"
           " WHERE historico_id IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1)")
    if n:
        problemas.append(f"importados com mais de um pedido: {n}")
    n = um("SELECT COUNT(*) FROM pedidos p WHERE p.historico_id IS NOT NULL"
           " AND NOT EXISTS (SELECT 1 FROM eventos_historico h"
           "                 WHERE h.id = p.historico_id)")
    if n:
        problemas.append(f"pedidos ligados a importado inexistente: {n}")
    return problemas


def executar(caminho: str, amostra: int, aplicar: bool):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Banco: {caminho}")
    if aplicar:
        backup = f"{caminho}.backup-antes-sprint21-{ts}"
        n = 1
        while Path(backup).exists():  # nunca sobrescreve um backup anterior
            n += 1
            backup = f"{caminho}.backup-antes-sprint21-{ts}-{n}"
        copiar_banco(caminho, backup)
        print(f"Backup criado e conferido: {backup}")
        alvo, tmp = caminho, None
    else:
        tmp = tempfile.TemporaryDirectory()
        alvo = str(Path(tmp.name) / "simulacao.db")
        copiar_banco(caminho, alvo)
        print("Modo simulação: alterações feitas numa cópia temporária."
              " O banco real não é modificado.")

    try:
        conn = usar_banco(alvo)
        imprimir_estrutura(conn)
        pre = dados.diagnostico_reclassificacao(conn)
        imprimir_resumo(pre, "PRÉ-CORREÇÃO")
        imprimir_detalhes(pre, amostra)
        antes = contagens(conn)

        titulo("ETAPA 3 — EXECUÇÃO" + ("" if aplicar else " (SIMULADA)"))
        resultado = dados.aplicar_reclassificacao(conn)
        print(f"Importados convertidos em pedidos atuais: {len(resultado['promovidos'])}")
        for x in resultado["promovidos"][:amostra]:
            print(f"  planilha #{x['numero_origem']} → pedido #{x['pedido_id']}")
        print(f"Marcações de histórico corrigidas: {len(resultado['desmarcados'])}"
              + (f" ({', '.join(f'#{i}' for i in resultado['desmarcados'])})"
                 if resultado["desmarcados"] else ""))
        reexecucao = dados.aplicar_reclassificacao(conn)
        depois = contagens(conn)

        pos = dados.diagnostico_reclassificacao(conn)
        imprimir_resumo(pos, "PÓS-CORREÇÃO")
        secao("Conferências")
        print(f"  reexecução alterou algo? {'SIM' if any(reexecucao.values()) else 'não'}")
        esperado = dict(antes, pedidos=antes["pedidos"] + len(resultado["promovidos"]))
        print(f"  registros: {antes} → {depois}")
        print("  apagados, duplicados ou itens inventados?"
              f" {'SIM' if depois != esperado else 'não'}")
        problemas = conferir(conn)
        for p in problemas:
            print(f"  PROBLEMA: {p}")
        if not problemas:
            print("  históricos fora da operação, um pedido por importado,"
                  " nenhum item inventado: ok")
        conn.close()
    finally:
        if tmp:
            tmp.cleanup()

    if aplicar:
        print(f"\nConcluído. Para desfazer: pare o serviço e restaure {backup}")
    else:
        print("\nSimulação concluída. Se o relatório estiver correto, rode com --aplicar.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true",
                    help="aplica no banco real (faz backup antes)")
    ap.add_argument("--amostra", type=int, default=30,
                    help="quantidade de registros exibidos nas listas")
    args = ap.parse_args()

    caminho = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")
    if not Path(caminho).exists():
        raise SystemExit(f"Banco não encontrado: {caminho}")
    if args.aplicar:
        resposta = input(f"Aplicar a reclassificação em {caminho}? Digite APLICAR: ")
        if resposta.strip() != "APLICAR":
            raise SystemExit("Cancelado.")
    executar(caminho, args.amostra, args.aplicar)


if __name__ == "__main__":
    main()
