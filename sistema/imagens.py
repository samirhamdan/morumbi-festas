"""Validação e preparo de imagens enviadas (logo e fundo do login).

O tipo é conferido pelo conteúdo do arquivo, nunca só pela extensão. Imagens
comuns são abertas e regravadas (sem metadados, tamanho limitado); SVG só é
aceito depois de passar por uma lista do que é permitido (sem scripts,
eventos, links externos ou conteúdo embutido).
"""

import io
import re
import xml.etree.ElementTree as ET

from PIL import Image, UnidentifiedImageError

LOGO_MAX_BYTES = 2 * 1024 * 1024
FUNDO_MAX_BYTES = 8 * 1024 * 1024
LOGO_MAX_PX = 1200
FUNDO_MAX_PX = 1920
# Proteção contra imagens "bomba" (dimensões absurdas em arquivo pequeno).
Image.MAX_IMAGE_PIXELS = 40_000_000


class ImagemInvalida(ValueError):
    pass


def _abrir(conteudo: bytes, formatos: tuple) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(conteudo))
        img.verify()  # confere a estrutura do arquivo
        img = Image.open(io.BytesIO(conteudo))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError):
        raise ImagemInvalida("Arquivo de imagem inválido ou corrompido.")
    if img.format not in formatos:
        raise ImagemInvalida("Formato de imagem não permitido.")
    return img


def _reduzir(img: Image.Image, maximo: int) -> Image.Image:
    if max(img.size) > maximo:
        img = img.copy()
        img.thumbnail((maximo, maximo), Image.LANCZOS)
    return img


def preparar_logo(conteudo: bytes, nome: str = "") -> tuple:
    """(bytes, extensão) prontos para gravar. PNG, JPG, WebP ou SVG, até 2 MB."""
    if not conteudo:
        raise ImagemInvalida("Arquivo vazio.")
    if len(conteudo) > LOGO_MAX_BYTES:
        raise ImagemInvalida("A logo deve ter no máximo 2 MB.")
    inicio = conteudo[:512].lstrip().lower()
    if inicio.startswith(b"<?xml") or inicio.startswith(b"<svg") or b"<svg" in inicio:
        return limpar_svg(conteudo), ".svg"
    img = _abrir(conteudo, ("PNG", "JPEG", "WEBP"))
    img = _reduzir(img, LOGO_MAX_PX)
    saida = io.BytesIO()
    if img.format == "JPEG" or (img.mode not in ("RGBA", "LA", "P") and img.format != "PNG"):
        img.convert("RGB").save(saida, "JPEG", quality=90, optimize=True)
        return saida.getvalue(), ".jpg"
    if img.mode not in ("RGBA", "LA", "RGB", "L", "P"):
        img = img.convert("RGBA")
    img.save(saida, "PNG", optimize=True)  # mantém a transparência
    return saida.getvalue(), ".png"


def preparar_fundo(conteudo: bytes) -> tuple:
    """Imagem de fundo comprimida em WebP (lado maior até 1920 px)."""
    if not conteudo:
        raise ImagemInvalida("Arquivo vazio.")
    if len(conteudo) > FUNDO_MAX_BYTES:
        raise ImagemInvalida("A imagem deve ter no máximo 8 MB.")
    img = _abrir(conteudo, ("PNG", "JPEG", "WEBP"))
    img = _reduzir(img.convert("RGB"), FUNDO_MAX_PX)
    saida = io.BytesIO()
    img.save(saida, "WEBP", quality=80, method=4)
    return saida.getvalue(), ".webp"


# --- SVG --------------------------------------------------------------------

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK = "http://www.w3.org/1999/xlink"
_ELEMENTOS_SVG = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "defs", "lineargradient", "radialgradient", "stop", "clippath",
    "mask", "use", "symbol", "title", "desc", "style", "pattern", "image", "filter",
    "fegaussianblur", "feoffset", "feblend", "fecolormatrix", "feflood", "fecomposite",
    "femerge", "femergenode", "metadata",
}


def _local(nome: str) -> str:
    return nome.rsplit("}", 1)[-1].lower()


def limpar_svg(conteudo: bytes) -> bytes:
    """SVG só com desenho: recusa scripts, eventos, links externos e entidades."""
    try:
        texto = conteudo.decode("utf-8")
    except UnicodeDecodeError:
        raise ImagemInvalida("SVG inválido (use codificação UTF-8).")
    if re.search(r"<!DOCTYPE|<!ENTITY", texto, re.I):
        raise ImagemInvalida("SVG com declarações não permitidas.")
    try:
        raiz = ET.fromstring(texto)
    except ET.ParseError:
        raise ImagemInvalida("SVG inválido.")
    if _local(raiz.tag) != "svg":
        raise ImagemInvalida("O arquivo não é um SVG.")
    for el in raiz.iter():
        if not isinstance(el.tag, str) or _local(el.tag) not in _ELEMENTOS_SVG:
            raise ImagemInvalida("SVG com elementos não permitidos.")
        if _local(el.tag) == "style" and re.search(r"@import|url\s*\(\s*['\"]?\s*(?!#)",
                                                    el.text or "", re.I):
            raise ImagemInvalida("SVG com estilos que carregam arquivos externos.")
        for nome, valor in el.attrib.items():
            atributo = _local(nome)
            valor_min = re.sub(r"\s", "", valor).lower()
            if atributo.startswith("on"):
                raise ImagemInvalida("SVG com scripts não é permitido.")
            if atributo == "href" and not (valor.startswith("#") or
                                           valor_min.startswith("data:image/png")
                                           or valor_min.startswith("data:image/jpeg")):
                raise ImagemInvalida("SVG com links externos não é permitido.")
            if "javascript:" in valor_min or ("url(" in valor_min and "url(#" not in valor_min):
                raise ImagemInvalida("SVG com conteúdo não permitido.")
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK)
    return ET.tostring(raiz, encoding="utf-8", xml_declaration=True)


FOTO_MAX_BYTES = 10 * 1024 * 1024
FOTO_MAX_PX = 1600
MINIATURA_PX = 480


def preparar_foto(conteudo: bytes) -> tuple:
    """Foto de produto/kit/categoria: (grande, miniatura) em WebP.

    A grande (até 1600 px) aparece no detalhe; a miniatura (até 480 px) nos
    cartões e listas — a vitrine nunca baixa a foto original.
    """
    if not conteudo:
        raise ImagemInvalida("Arquivo vazio.")
    if len(conteudo) > FOTO_MAX_BYTES:
        raise ImagemInvalida("A foto deve ter no máximo 10 MB.")
    img = _abrir(conteudo, ("PNG", "JPEG", "WEBP"))
    if img.mode in ("RGBA", "LA", "P"):
        fundo = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        fundo.paste(rgba, mask=rgba.split()[-1])
        img = fundo
    else:
        img = img.convert("RGB")
    saidas = []
    for limite, qualidade in ((FOTO_MAX_PX, 82), (MINIATURA_PX, 78)):
        copia = _reduzir(img, limite)
        b = io.BytesIO()
        copia.save(b, "WEBP", quality=qualidade, method=4)
        saidas.append(b.getvalue())
    return tuple(saidas)
