"""Researcher-side factorial selection and material preview; never model input."""
from collections import Counter

from ..schemas import ErrorCondition

COHORT_NAMES = {"all80": "全部80份", "old40": "旧40份 · 开发材料", "new40": "新40份 · 独立材料"}


class ExperimentDesign:
    def __init__(self, registry, case_ids, cohort_rows=()):
        self.registry, self.case_ids = registry, tuple(case_ids)
        self.cohort = {r["case_id"]: ("old40" if r["cohort"] == "old40_development" else "new40")
                       for r in cohort_rows if r["cohort"] in ("old40_development", "new40_main")}
        for cid in self.case_ids:
            self.cohort.setdefault(cid, "old40")

    def cases(self, cohort="all80"):
        if not isinstance(cohort, str) or cohort not in COHORT_NAMES:
            raise ValueError("请选择旧40份、新40份或全部80份")
        return tuple(cid for cid in self.case_ids if cohort == "all80" or self.cohort[cid] == cohort)

    @staticmethod
    def dimension(data, key, allowed):
        values = data.get(key, list(allowed))
        if (not isinstance(values, list) or not values or not all(isinstance(x, str) for x in values)
                or len(set(values)) != len(values) or not set(values) <= set(allowed)):
            raise ValueError(f"{key} 必须选择至少一个有效且不重复的条件")
        return [x for x in allowed if x in values]

    def estimate(self, data):
        if not isinstance(data, dict):
            raise ValueError("实验参数必须是对象")
        if set(data) - {"a", "v", "d", "e", "cohort", "repeat", "case_ids"}:
            raise ValueError("包含不支持的实验参数，请刷新页面后重试")
        factors = {key: self.dimension(data, key, allowed) for key, allowed in
                   (("a", ("A0", "A1")), ("v", ("V0", "V1")),
                    ("d", ("D0", "D1", "D2")), ("e", ("E0", "E1")))}
        repeat = data.get("repeat", 1)
        if type(repeat) is not int or not 1 <= repeat <= 10:
            raise ValueError("重复次数必须为1到10的整数")
        cohort = data.get("cohort", "all80")
        cases = self.cases(cohort)
        selected = data.get("case_ids")
        if selected is not None:
            if (not isinstance(selected, list) or not selected or not all(isinstance(x, str) for x in selected)
                    or len(set(selected)) != len(selected) or not set(selected) <= set(cases)):
                raise ValueError("所选合同必须属于当前材料范围，且不能重复")
            cases = tuple(cid for cid in cases if cid in selected)
        conditions = [a + v + d for a in factors["a"] for v in factors["v"] for d in factors["d"]
                      if a + v != "A0V1"]
        excluded = ["A0V1" + d for d in factors["d"]] if "A0" in factors["a"] and "V1" in factors["v"] else []
        counts = Counter()
        eligible = []
        for cid in cases:
            arms = [e.value for e in self.registry.get(cid).error_conditions if e.value in factors["e"]]
            if arms:
                eligible.append(cid)
                counts.update(arms)
        memo_count = sum(counts.values())
        return dict(**factors, cohort=cohort, repeat=repeat, case_ids=eligible,
                    requested_contracts=len(cases), contracts=len(eligible), memo_counts=dict(counts),
                    memo_count=memo_count, conditions=conditions, condition_count=len(conditions),
                    excluded_conditions=excluded, skipped_contracts=len(cases) - len(eligible),
                    total=memo_count * len(conditions) * repeat,
                    formula=f"{memo_count} 份可用memo × {len(conditions)} 组A/V/D × {repeat} 次重复",
                    valid=bool(memo_count and conditions))

    def catalog(self, cohort="all80"):
        return [dict(case_id=cid, cohort=self.cohort[cid], characters=len(self.registry.get(cid).contract_text),
                     memo_variants=[e.value for e in self.registry.get(cid).error_conditions],
                     claims=len(self.registry.memo(cid, ErrorCondition.E0).claims)) for cid in self.cases(cohort)]

    def preview(self, case_id):
        if case_id not in self.case_ids:
            raise ValueError("合同不在可用材料范围内")
        case = self.registry.get(case_id)
        memos = {e.value: self.registry.memo(case_id, e).model_dump(mode="json") for e in case.error_conditions}
        ids = {pid for memo in memos.values() for claim in memo["claims"] for pid in claim["source_ids"]}
        doc = self.registry.document(case_id)
        sources = {p.paragraph_id: p.text for p in doc.paragraphs if p.paragraph_id in ids}
        return dict(case_id=case_id, cohort=self.cohort[case_id], contract_text=case.contract_text,
                    contract_text_hash=case.contract_text_hash, memos=memos, cited_sources=sources,
                    memo_variants=list(memos), review="AI审核冻结；人工复核尚未完成",
                    note="研究者预览，不会作为额外信息发给实验agent；预览不调用模型。")
