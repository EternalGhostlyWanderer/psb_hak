"""Офлайн-проверка поиска (без API): находит ли поиск нужный документ.

Для каждого из 18 демо-вопросов указан документ, на который ссылается
эталонный ответ организаторов (из demo_output.csv). Считаем:
- hit@5 по документам: нужный документ попал в топ-5 выдачи чанков;
- для вопросов с известным пунктом — попал ли сам пункт в топ-8.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.kb import load_chunks
from core.search import HybridSearcher
from core.dialogue import parse_question

# id вопроса -> (подстрока doc_id правильного документа, ожидаемый пункт или "")
EXPECTED = {
    "1":  ("Договор аренды А-08-2024", "п. 3.1"),
    "2":  ("ТК_ст70", ""),
    "3":  (None, ""),                       # ответа в базе нет -> отказ
    "4":  ("Договор аренды А-08-2024", "п. 3.1"),
    "5":  ("Регламент согласования договоров ред 2023", "п. 2.4"),
    "6":  ("Договор поставки П-17-2024", "п. 6.1"),
    "7":  ("Договор аренды А-08-2024", ""),
    "8":  ("Договор поставки П-17-2024", ""),
    "9":  ("Договор поставки ПТ-05-2023", ""),
    "10": ("Регламент согласования договоров ред 2025", "п. 1.3"),
    "11": ("Служебная записка", ""),
    "12": ("ТК_ст70", ""),
    "13": ("152ФЗ_ст9", ""),
    "14": ("ТК_ст59", ""),
    "15": ("Договор аренды А-08-2024", "п. 6.3"),
    "16": ("Договор поставки П-17-2024", "п. 2.1"),
    "17": ("Договор поставки ПТ-05-2023", "п. 6.1"),
    "18": ("Регламент согласования договоров ред 2025", "п. 3.2"),
}


# Свои вопросы: 18 демо почти не трогают НК и КоАП, а это 2/3 базы.
# (вопрос, подстрока doc_id, ожидаемый пункт или "")
EXTRA = [
    ("Какая ставка НДС действует на детские товары?", "НК_ст164", "п. 2"),
    ("Какая основная ставка налога на прибыль организаций?", "НК_ст284", "п. 1"),
    ("Какой штраф грозит юридическому лицу за обработку персональных данных без письменного согласия?", "КоАП_ст13.11", "ч. 2"),
    ("Можно ли привлечь работника к сверхурочной работе без его согласия, чтобы предотвратить аварию?", "ТК_ст99", ""),
    ("В каком размере оплачивается сверхурочная работа?", "ТК_ст152", ""),
    ("По какой ставке НДФЛ облагаются доходы нерезидентов?", "НК_ст224", "п. 3"),
    ("Какие обстоятельства смягчают ответственность за налоговое правонарушение?", "НК_ст112", "п. 1"),
    ("При какой выручке организация может получить освобождение от обязанностей плательщика НДС?", "НК_ст145", "п. 1"),
    ("Какой минимальный размер оплаты труда установлен?", "МРОТ_ст1", ""),
    ("Нужна ли санкционная проверка перед договором с иностранным контрагентом?", "Письмо комплаенс", ""),
    ("По какому адресу Альфа-Трейд должен доставить мебель по спецификации от ноября 2024 года?", "Доп соглашение", ""),
    ("Во сколько раз увеличивается штраф при повторном налоговом правонарушении?", "НК_ст114", "п. 4"),
    ("Что должно быть указано в письменном согласии на обработку персональных данных?", "152ФЗ_ст9", "ч. 4"),
    ("Какая основная ставка НДС?", "НК_ст164", "п. 3"),
    ("Сколько НДФЛ платит резидент с зарплаты?", "НК_ст224", "п. 1"),
    ("Договор на 5 млн рублей: с кем из руководства его нужно согласовать?", "Регламент согласования договоров ред 2025", "п. 2.3"),
]


def main():
    chunks = load_chunks(Path(__file__).resolve().parents[1] / "data/chunks.json")
    searcher = HybridSearcher(chunks)  # локально: только BM25 + эвристики

    with open(Path(__file__).resolve().parents[1] / "data/demo_questions.csv",
              encoding="utf-8-sig") as f:
        questions = {r["id"]: r["question"] for r in csv.DictReader(f)}

    doc_hits = clause_hits = clause_total = 0
    applicable = 0
    for qid, raw in questions.items():
        expected_doc, expected_clause = EXPECTED[qid]
        question, history = parse_question(raw)
        query = question
        if history:
            query = " ".join(t["content"] for t in history[-2:]) + " " + question
        results = searcher.search(query, top_k=8)

        if expected_doc is None:
            top_titles = {c.doc_title for c in results[:3]}
            print(f"[{qid:>2}] (нет ответа в базе) топ-3: {sorted(top_titles)}")
            continue
        applicable += 1

        top5_docs = [c.doc_id for c in results[:5]]
        doc_ok = any(expected_doc in d for d in top5_docs)
        doc_hits += doc_ok

        clause_ok = None
        if expected_clause:
            clause_total += 1
            clause_ok = any(expected_doc in c.doc_id and c.clause == expected_clause
                            for c in results)
            clause_hits += bool(clause_ok)

        mark = "OK " if doc_ok else "MISS"
        cl = "" if clause_ok is None else ("  пункт:OK" if clause_ok else f"  пункт:MISS({expected_clause})")
        print(f"[{qid:>2}] {mark} ждём «{expected_doc}»{cl}")
        if not doc_ok:
            for c in results[:5]:
                print(f"       got: {c.doc_title} {c.clause}")

    print("\n--- свои вопросы по законам и документам ---")
    for question, expected_doc, expected_clause in EXTRA:
        results = searcher.search(question, top_k=8)
        applicable += 1
        doc_ok = any(expected_doc in c.doc_id for c in results[:5])
        doc_hits += doc_ok
        cl = ""
        if expected_clause:
            clause_total += 1
            # у длинного пункта чанки названы «п. 2, пп. 1» — засчитываем по началу метки
            ok = any(expected_doc in c.doc_id and
                     (c.clause == expected_clause or c.clause.startswith(expected_clause + ","))
                     for c in results)
            clause_hits += ok
            cl = "  пункт:OK" if ok else f"  пункт:MISS({expected_clause})"
        print(f"{'OK ' if doc_ok else 'MISS'} {question[:70]}{cl}")
        if not doc_ok or "MISS" in cl:
            for c in results[:6]:
                print(f"       got: {c.ref()}")

    print(f"\nДокумент в топ-5: {doc_hits}/{applicable}")
    print(f"Нужный пункт в топ-8: {clause_hits}/{clause_total}")


if __name__ == "__main__":
    main()
