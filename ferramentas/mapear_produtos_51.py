"""Sprint 5.1 — mapa dos produtos e kits atuais (SOMENTE LEITURA).

Não altera o banco. Lista cada produto com uma SUGESTÃO de tipo (locação,
sob encomenda, serviço) pelo nome, onde ele é usado (kits, orçamentos,
pedidos) e o que é ambíguo. Serve para decidir a classificação antes da
migração do Sprint 5.1.

Uso na VPS (o banco é o da variável FESTAS_DADOS do serviço):
    cd /opt/morumbi-festas
    BANCO=$(systemctl show morumbi-festas -p Environment | grep -o 'FESTAS_DADOS=[^ ]*' | cut -d= -f2)
    venv/bin/python ferramentas/mapear_produtos_51.py "$BANCO" > mapa51.txt
"""

import re
import sqlite3
import sys
import unicodedata

PALAVRAS = {
    "encomenda": ("balao", "baloes", "guirlanda", "arco", "coluna de bal", "desconstruid",
                  "bubble", "personalizad"),
    "servico": ("montagem", "instalacao", "desmontagem", "frete", "entrega", "deslocamento",
                "mao de obra", "servico"),
}


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def sugerir(nome: str, descricao: str) -> tuple:
    texto = _norm(f"{nome} {descricao}")
    achados = {tipo for tipo, palavras in PALAVRAS.items()
               if any(re.search(r"\b" + re.escape(p), texto) for p in palavras)}
    if len(achados) > 1:
        return "ambiguo", "nome indica mais de um tipo: " + ", ".join(sorted(achados))
    if achados:
        return achados.pop(), "palavra-chave no nome/descrição"
    return "locacao", "sem palavra-chave (o cadastro atual é de locação)"


def main(caminho: str):
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'produtos'").fetchone():
        sys.exit(f"{caminho} não é o banco do sistema (não tem a tabela de produtos).\n"
                 "O caminho certo está em FESTAS_DADOS:\n"
                 "    systemctl show morumbi-festas -p Environment")
    q = lambda sql, *p: conn.execute(sql, p).fetchall()  # noqa: E731
    produtos = q("SELECT p.*, c.nome AS categoria FROM produtos p"
                 " LEFT JOIN categorias c ON c.id = p.categoria_id ORDER BY p.tenant_id, p.nome")
    print(f"Produtos: {len(produtos)} | Kits: {q('SELECT COUNT(*) FROM kits')[0][0]}"
          f" | Itens de kit: {q('SELECT COUNT(*) FROM itens_kit')[0][0]}")
    print("Itens de pedido por tipo:", {r[0]: r[1] for r in q(
        "SELECT tipo, COUNT(*) FROM itens_pedido GROUP BY tipo")})
    print("Itens de orçamento por tipo:", {r[0]: r[1] for r in q(
        "SELECT tipo, COUNT(*) FROM itens_orcamento GROUP BY tipo")})
    fracionados = q("SELECT COUNT(*) FROM itens_pedido WHERE quantidade != CAST(quantidade AS INTEGER)")
    print("Quantidades fracionadas já gravadas em pedidos:", fracionados[0][0])
    print()
    contagem: dict = {}
    print("ID | SUGESTÃO | NOME | SKU | CATEGORIA | ESTOQUE | PREÇO | STATUS | EM KITS | EM PEDIDOS | EM ORÇAMENTOS | MOTIVO")
    for p in produtos:
        tipo, motivo = sugerir(p["nome"], p["descricao"])
        contagem[tipo] = contagem.get(tipo, 0) + 1
        em_kits = q("SELECT COUNT(DISTINCT kit_id) FROM itens_kit WHERE produto_id = ?", p["id"])[0][0]
        em_ped = q("SELECT COUNT(*) FROM itens_pedido WHERE tipo = 'produto' AND item_id = ?", p["id"])[0][0]
        em_orc = q("SELECT COUNT(*) FROM itens_orcamento WHERE tipo = 'produto' AND item_id = ?", p["id"])[0][0]
        print(f"{p['id']} | {tipo.upper()} | {p['nome']} | {p['codigo_sku']} | {p['categoria'] or '-'}"
              f" | {p['quantidade_total']} | {p['preco_locacao']:.2f} | {p['status']} | {em_kits}"
              f" | {em_ped} | {em_orc} | {motivo}")
    print()
    print("Resumo das sugestões:", contagem)
    print()
    print("Serviços digitados em pedidos (texto livre, sem cadastro):")
    for r in q("SELECT descricao, COUNT(*) AS n, MIN(preco_unitario) AS mn, MAX(preco_unitario) AS mx"
               " FROM itens_pedido WHERE tipo = 'servico' GROUP BY descricao ORDER BY n DESC LIMIT 40"):
        print(f"  {r['descricao']} — {r['n']} vez(es), preço de {r['mn']:.2f} a {r['mx']:.2f}")
    print()
    print("Kits e suas peças:")
    for k in q("SELECT * FROM kits ORDER BY nome"):
        pecas = q("SELECT p.nome, ik.quantidade FROM itens_kit ik JOIN produtos p ON p.id = ik.produto_id"
                  " WHERE ik.kit_id = ?", k["id"])
        print(f"  {k['nome']} ({k['status']}, R$ {k['preco']:.2f}): "
              + (", ".join(f"{r['quantidade']}x {r['nome']}" for r in pecas) or "sem peças"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
