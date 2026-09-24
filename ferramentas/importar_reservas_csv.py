#!/usr/bin/env python3
"""Importa reservas do formulario Google (CSV) para eventos_historico.

Vincula cada reserva ao cliente existente no Morumbi Festas por email
ou nome normalizado. Nao cria clientes novos.

Idempotente: usa (origem='Formulario Festas', origem_id=<linha CSV>)
como chave unica.

Uso:
    FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \
        python ferramentas/importar_reservas_csv.py <caminho_csv>
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FUSO = timezone(timedelta(hours=-4))
CAMINHO_FESTAS = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")
ORIGEM = "Formulario Festas"


def agora() -> str:
    return datetime.now(FUSO).strftime("%Y-%m-%d %H:%M:%S")


def _normalizar(nome: str) -> str:
    nome = nome.strip().lower()
    nome = unicodedata.normalize("NFKD", nome)
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    return " ".join(nome.split())


def _normalizar_email(email: str) -> str:
    return email.strip().lower().rstrip()


def _parse_data(valor: str, mes: str = "", ano: str = "",
                carimbo: str = "") -> str | None:
    """Extrai data YYYY-MM-DD de varios formatos."""
    valor = valor.strip()

    if not valor or valor.lower() == "aberto":
        if mes and ano:
            try:
                m = int(mes)
                a = int(ano)
                if 2020 <= a <= 2030 and 1 <= m <= 12:
                    return f"{a}-{m:02d}-15"
            except (ValueError, TypeError):
                pass
        if carimbo:
            try:
                return carimbo[:10].replace("/", "-")
            except Exception:
                pass
        return None

    # DD/MM/YYYY
    m_dmy = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", valor)
    if m_dmy:
        d, mo, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        if 1 <= d <= 31 and 1 <= mo <= 12 and 2020 <= y <= 2030:
            return f"{y}-{mo:02d}-{d:02d}"

    # YYYY/MM/DD ou 00YY/MM/DD (typos)
    m_ymd = re.match(r"^0*(\d{4})/(\d{1,2})/(\d{1,2})", valor)
    if m_ymd:
        y, mo, d = int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3))
        if y < 100:
            y += 2000
        if y < 2020 and carimbo:
            try:
                carimbo_ano = int(carimbo[:4])
                if 2020 <= carimbo_ano <= 2030:
                    y = carimbo_ano
            except (ValueError, IndexError):
                pass
        if y < 2020:
            y = 2023
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y}-{mo:02d}-{d:02d}"

    return None


def _mapear_status(status_csv: str) -> str:
    s = status_csv.strip().lower()
    if s == "finalizado":
        return "entregue"
    if s == "agendado":
        return "aprovado"
    return "aprovado"


def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(CAMINHO_FESTAS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _mapa_email(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT id, email FROM clientes WHERE email IS NOT NULL AND email != ''").fetchall()
    mapa = {}
    for r in rows:
        chave = _normalizar_email(r["email"])
        if chave and chave not in mapa:
            mapa[chave] = r["id"]
    return mapa


def _mapa_nome(conn: sqlite3.Connection) -> dict[str, int]:
    """Nome normalizado -> cliente. Nomes repetidos ficam de fora: sem prova
    de identidade a reserva vai para SEM CORRESPONDENCIA (nunca por palpite)."""
    rows = conn.execute("SELECT id, nome FROM clientes").fetchall()
    mapa: dict[str, int] = {}
    repetidos: set[str] = set()
    for r in rows:
        chave = _normalizar(r["nome"])
        if not chave:
            continue
        if chave in mapa and mapa[chave] != r["id"]:
            repetidos.add(chave)
        mapa.setdefault(chave, r["id"])
    for chave in repetidos:
        del mapa[chave]
    return mapa


def _ja_importados(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT origem_id FROM eventos_historico WHERE origem = ?",
        (ORIGEM,)).fetchall()
    return {r["origem_id"] for r in rows}


def importar(caminho_csv: str) -> dict:
    conn = conectar()

    from sistema.dados import inicializar
    inicializar()

    mapa_email = _mapa_email(conn)
    mapa_nome = _mapa_nome(conn)
    ja = _ja_importados(conn)
    ts = agora()

    importados = 0
    ignorados_dup = 0
    sem_correspondencia = []
    sem_data_evento = []
    vinculados_email = 0
    vinculados_nome = 0
    por_status: dict[str, int] = {}
    linhas_invalidas = 0

    with open(caminho_csv, encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)

        for idx, row in enumerate(reader, start=2):
            if len(row) < 9:
                linhas_invalidas += 1
                continue

            nome = row[1].strip()
            if not nome:
                linhas_invalidas += 1
                continue

            origem_id = idx
            if origem_id in ja:
                ignorados_dup += 1
                continue

            email = row[3].strip() if len(row) > 3 else ""
            kit = row[6].strip() if len(row) > 6 else ""
            data_raw = row[8].strip() if len(row) > 8 else ""
            pagamento = row[9].strip() if len(row) > 9 else ""
            canal = row[10].strip() if len(row) > 10 else ""
            mes = row[11].strip() if len(row) > 11 else ""
            ano = row[12].strip() if len(row) > 12 else ""
            status_csv = row[13].strip() if len(row) > 13 else ""
            carimbo = row[0].strip()

            cliente_id = None
            metodo = ""

            if email:
                email_norm = _normalizar_email(email)
                cliente_id = mapa_email.get(email_norm)
                if cliente_id:
                    metodo = "email"
                    vinculados_email += 1

            if not cliente_id:
                nome_norm = _normalizar(nome)
                cliente_id = mapa_nome.get(nome_norm)
                if cliente_id:
                    metodo = "nome"
                    vinculados_nome += 1

            if not cliente_id:
                sem_correspondencia.append({
                    "linha": idx,
                    "nome": nome,
                    "email": email,
                    "kit": kit,
                    "data_raw": data_raw,
                    "status": status_csv,
                })
                continue

            data_evento = _parse_data(data_raw, mes, ano, carimbo)
            if not data_evento:
                sem_data_evento.append({
                    "linha": idx, "nome": nome,
                    "data_raw": data_raw, "mes": mes, "ano": ano,
                })

            status_origem = _mapear_status(status_csv)

            descricao = kit if kit else nome
            obs_parts = []
            if pagamento:
                obs_parts.append(f"Pgto: {pagamento}")
            if not canal:
                canal = ""

            conn.execute(
                "INSERT INTO eventos_historico"
                " (cliente_id, origem_id, origem, data_evento, descricao,"
                "  observacoes, canal, valor, status_origem, criado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cliente_id, origem_id, ORIGEM, data_evento,
                 descricao, "; ".join(obs_parts), canal,
                 0, status_origem, ts))

            importados += 1
            por_status[status_origem] = por_status.get(status_origem, 0) + 1

    conn.commit()

    class_result = _atualizar_classificacoes(conn)
    conn.close()

    return {
        "importados": importados,
        "ignorados_duplicados": ignorados_dup,
        "vinculados_email": vinculados_email,
        "vinculados_nome": vinculados_nome,
        "sem_correspondencia": sem_correspondencia,
        "sem_data_evento": sem_data_evento,
        "por_status": por_status,
        "linhas_invalidas": linhas_invalidas,
        "clientes_atualizados": class_result["clientes_atualizados"],
    }


def _atualizar_classificacoes(conn: sqlite3.Connection) -> dict:
    """Usa a mesma regra de festas realizadas do sistema (sistema.dados)."""
    from sistema.dados import atualizar_todas_classificacoes

    conn.commit()
    return {"clientes_atualizados": atualizar_todas_classificacoes()}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python ferramentas/importar_reservas_csv.py <caminho_csv>")
        sys.exit(1)

    caminho = sys.argv[1]
    if not Path(caminho).exists():
        print(f"Arquivo nao encontrado: {caminho}")
        sys.exit(1)

    ts = datetime.now(FUSO).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] Importando reservas do CSV...")
    print(f"  CSV: {caminho}")
    print(f"  Festas: {CAMINHO_FESTAS}")

    resultado = importar(caminho)

    print(f"\n  Importados: {resultado['importados']}")
    print(f"  Ignorados (duplicados): {resultado['ignorados_duplicados']}")
    print(f"  Vinculados por email: {resultado['vinculados_email']}")
    print(f"  Vinculados por nome: {resultado['vinculados_nome']}")
    print(f"  Linhas invalidas: {resultado['linhas_invalidas']}")

    sem = resultado["sem_correspondencia"]
    if sem:
        print(f"\n  SEM CORRESPONDENCIA ({len(sem)} registros):")
        for r in sem:
            print(f"    Linha {r['linha']}"
                  f" | {r['nome']}"
                  f" | {r['email']}"
                  f" | {r['status']}")

    sem_data = resultado["sem_data_evento"]
    if sem_data:
        print(f"\n  SEM DATA DE EVENTO ({len(sem_data)} registros):")
        for r in sem_data:
            print(f"    Linha {r['linha']}"
                  f" | {r['nome']}"
                  f" | raw='{r['data_raw']}'"
                  f" | mes={r['mes']} ano={r['ano']}")

    if resultado["por_status"]:
        print(f"\n  Por status:")
        for s, c in sorted(resultado["por_status"].items()):
            print(f"    {s}: {c}")

    print(f"  Classificacoes atualizadas: {resultado['clientes_atualizados']}")
    print("  Concluido.")
    print("\n  Festas futuras importadas aparecem em Pedidos > Em andamento como"
          " importadas.\n  Para converte-las em pedidos atuais, rode"
          " ferramentas/reclassificar_pedidos.py.")
