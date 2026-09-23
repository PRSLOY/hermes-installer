#!/usr/bin/env python3
"""Достаёт текст из PDF, скана или фото документа. Страницы без текстового
слоя распознаёт встроенным OCR библиотеки PyMuPDF (нужны только файлы языков
Tesseract, сама программа Tesseract не нужна).

Примеры:
    python scan_to_text.py договор.pdf --check
    python scan_to_text.py договор.pdf --out договор.txt
    python scan_to_text.py фото.jpg --lang rus --out фото.txt
    python scan_to_text.py отчёт.pdf --pages 1-3
    python scan_to_text.py скан.pdf --pages 2 --render страницы

Нумерация страниц в --pages с единицы, как в программах просмотра PDF.
Оригинал никогда не изменяется.
"""
import argparse
import json
import os
import sys

EMPTY_PAGE_CHARS = 20  # меньше символов на странице = считаем страницу сканом


def default_tessdata():
    base = os.environ.get("HERMES_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"
    )
    return os.path.join(base, "doc-tools", "tessdata")


def open_document(path):
    import pymupdf

    doc = pymupdf.open(path)
    if not doc.is_pdf:  # картинка (jpg/png/tiff...) -> одностраничный PDF в памяти
        doc = pymupdf.open("pdf", doc.convert_to_pdf())
    return doc


def parse_pages(spec, total):
    if not spec:
        return list(range(total))
    result = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            result.extend(range(int(a) - 1, int(b)))
        else:
            result.append(int(part) - 1)
    return [i for i in result if 0 <= i < total]


def check(doc):
    empty = [i + 1 for i, page in enumerate(doc) if len(page.get_text().strip()) < EMPTY_PAGE_CHARS]
    return {"pages": len(doc), "pages_without_text": empty}


def main():
    parser = argparse.ArgumentParser(description="Текст из PDF/скана/фото с OCR для страниц без текста.")
    parser.add_argument("path", help="PDF или картинка (jpg, png, tiff)")
    parser.add_argument("--out", help="куда сохранить текст (UTF-8); без него — вывод на экран")
    parser.add_argument("--lang", default="rus+eng", help="языки OCR, по умолчанию rus+eng")
    parser.add_argument("--tessdata", default=None, help="папка с файлами языков *.traineddata")
    parser.add_argument("--pages", help="страницы, например 1-3 или 1,4,7 (с единицы)")
    parser.add_argument("--dpi", type=int, default=300, help="разрешение для OCR, по умолчанию 300")
    parser.add_argument("--force-ocr", action="store_true", help="распознавать даже страницы с текстом")
    parser.add_argument("--check", action="store_true", help="только показать, на каких страницах нет текста")
    parser.add_argument("--render", metavar="DIR", help="сохранить страницы картинками PNG (150 dpi) в папку DIR")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        import pymupdf  # noqa: F401
    except ImportError:
        print("Не установлен pymupdf. Установку делать только с согласия человека (см. SKILL.md).", file=sys.stderr)
        return 2

    doc = open_document(args.path)

    if args.check:
        print(json.dumps(check(doc), ensure_ascii=False))
        return 0

    if args.render:
        os.makedirs(args.render, exist_ok=True)
        saved = []
        for i in parse_pages(args.pages, len(doc)):
            target = os.path.join(args.render, f"page-{i + 1}.png")
            doc[i].get_pixmap(dpi=150).save(target)
            saved.append(target)
        print(json.dumps({"images": saved}, ensure_ascii=False))
        return 0

    tessdata = args.tessdata or default_tessdata()
    langs = [l for l in args.lang.split("+") if l]
    missing = [l for l in langs if not os.path.isfile(os.path.join(tessdata, l + ".traineddata"))]

    chunks, ocr_pages, still_empty = [], [], []
    for i in parse_pages(args.pages, len(doc)):
        page = doc[i]
        text = page.get_text()
        if args.force_ocr or len(text.strip()) < EMPTY_PAGE_CHARS:
            if missing:
                print(
                    f"Страница {i + 1} без текста, а файлов языков нет: {', '.join(missing)} "
                    f"в {tessdata}. OCR пропущен.",
                    file=sys.stderr,
                )
            else:
                tp = page.get_textpage_ocr(language=args.lang, dpi=args.dpi, full=True, tessdata=tessdata)
                text = page.get_text(textpage=tp)
                ocr_pages.append(i + 1)
        if len(text.strip()) < EMPTY_PAGE_CHARS:
            still_empty.append(i + 1)
        chunks.append(f"--- Страница {i + 1} из {len(doc)} ---\n{text.strip()}\n")

    output = "\n".join(chunks)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
    else:
        print(output)

    print(
        json.dumps(
            {"pages": len(doc), "ocr_pages": ocr_pages, "empty_after_ocr": still_empty, "out": args.out},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
