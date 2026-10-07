"""Extract explicitly labelled requirements without substituting classification text."""

import re

from app.models.domain import DocumentData, LotData

LABELS = {
    "warranty": ["Гарантия", "Гарантийный срок", "Гарантийные обязательства"],
    "certificates": ["Сертификаты", "Требования к сертификатам"],
    "installation_requirements": ["Требования к монтажу", "Требования к установке", "Монтаж"],
    "payment_terms": ["Условия оплаты", "Порядок оплаты"],
    "additional_conditions": ["Дополнительные условия"],
    "execution_period": ["Срок выполнения", "Срок исполнения", "Срок поставки"],
    "required_deliverables": ["Комплект поставки", "Требуемые результаты", "Результат работ"],
}


def attach_specification(lot: LotData, summary_characters: int) -> LotData:
    texts = [lot.specification_original] if lot.specification_original else []

    def specification_documents(documents: list[DocumentData]):
        for document in documents:
            if document.is_specification:
                yield document
            else:
                yield from specification_documents(document.children)

    for document in specification_documents(lot.documents):
        if document.is_specification and document.original_text.strip():
            texts.append(
                f"Источник: {document.url}\nФайл: {document.name or ''}\n{document.original_text}"
            )
    lot.specification_original = "\n\n".join(dict.fromkeys(texts))
    # An extractive preview only; the full text always survives unchanged.
    original = lot.specification_original
    lot.specification_summary = original[:summary_characters] + (
        "…" if len(original) > summary_characters else ""
    )
    if original:
        lot.technical_characteristics = original
    for field, labels in LABELS.items():
        if getattr(lot, field):
            continue
        values = []
        for label in labels:
            values.extend(re.findall(rf"(?im)^\s*{re.escape(label)}\s*[:—-]\s*(.+)$", original))
        if values:
            setattr(lot, field, "\n".join(dict.fromkeys(values)))
    return lot
