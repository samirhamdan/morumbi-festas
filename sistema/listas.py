"""Ordenacao e busca para listas do sistema."""

from unicodedata import normalize


def _normalizar(texto: str) -> str:
    return normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()


def filtrar(lista: list, busca: str, campos: tuple) -> list:
    if not busca:
        return lista
    termos = _normalizar(busca).split()
    resultado = []
    for item in lista:
        texto = " ".join(_normalizar(str(item.get(c, "") or "")) for c in campos)
        if all(t in texto for t in termos):
            resultado.append(item)
    return resultado


def ordenar(lista: list, chave: str, permitidos: tuple, inverter: bool = False) -> list:
    if not chave:
        return lista
    for nome, _, fn in permitidos:
        if nome == chave:
            return sorted(lista, key=lambda x: fn(x), reverse=inverter)
    return lista


ORDENS_USUARIOS = (
    ("nome", "Nome", lambda u: (u.get("nome") or "").lower()),
    ("perfil", "Perfil", lambda u: (u.get("perfil") or "").lower()),
    ("login", "Login", lambda u: (u.get("login") or "").lower()),
)

BUSCA_USUARIOS = ("nome", "login", "perfil")
