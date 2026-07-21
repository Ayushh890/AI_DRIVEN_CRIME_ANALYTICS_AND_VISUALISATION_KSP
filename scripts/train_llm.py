"""
LoRA fine-tuning recipe for the KSP CIP LLM assistant.

This script prepares a schema-grounded text-to-SQL training set from the KSP
schema description and a curated question/SQL bank, then runs a LoRA fine-tune
of an open-source base model (default: `microsoft/phi-3-mini-4k-instruct`).

Run on a workstation with a CUDA GPU (>= 16 GB VRAM recommended). The trained
adapter is saved to `models/ksp-sql-adapter/` and can be loaded at inference
by exporting KSP_LLM_BACKEND=ollama (after converting to GGUF) or by pointing
an OpenAI-compatible server (vLLM / llama.cpp) at the merged model.

Requirements (install locally, NOT in the pilot's runtime image):
    pip install torch transformers peft datasets accelerate bitsandbytes

Usage:
    python scripts/train_llm.py \
        --base microsoft/phi-3-mini-4k-instruct \
        --out models/ksp-sql-adapter \
        --epochs 3

To add your own supervised examples, edit CURATED_QA below or drop a
data/qa.jsonl file with one {"question","sql","explanation"} record per line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The schema summary is duplicated (from backend/llm.py) so training doesn't
# need the runtime deps.
SCHEMA_SUMMARY = Path(__file__).parent.parent / "backend" / "llm.py"

SYSTEM_PROMPT_TMPL = """You are a SQL analyst for the Karnataka Police State Crime Records Bureau.
You answer questions about crime by writing SQLite queries against the KSP FIR schema.

RULES (strict):
  * Emit ONE valid SQLite SELECT statement.
  * Never modify data (no INSERT / UPDATE / DELETE / DROP / ALTER / CREATE / PRAGMA / ATTACH / VACUUM).
  * Use only tables from the schema below.
  * Column names are case-sensitive; use them exactly as shown.
  * Karnataka's StateID is 29.
  * Respond as strict JSON: {{"sql": "...", "explanation": "..."}}. No prose outside the JSON.

Schema:
{schema}"""

CURATED_QA = [
    {
        "question": "top 20 offenders by number of cases",
        "sql": "SELECT a.person_link_id, a.AccusedName, COUNT(DISTINCT a.CaseMasterID) AS cases FROM Accused a WHERE a.person_link_id IS NOT NULL GROUP BY a.person_link_id, a.AccusedName ORDER BY cases DESC LIMIT 20",
        "explanation": "Top offenders by distinct case count",
    },
    {
        "question": "which districts have the highest heinous crime count?",
        "sql": "SELECT d.DistrictName, COUNT(*) AS heinous_cases FROM CaseMaster cm JOIN Unit u ON u.UnitID=cm.PoliceStationID JOIN District d ON d.DistrictID=u.DistrictID JOIN GravityOffence g ON g.GravityOffenceID=cm.GravityOffenceID WHERE g.LookupValue='Heinous' GROUP BY d.DistrictName ORDER BY heinous_cases DESC LIMIT 15",
        "explanation": "District-level heinous case ranking",
    },
    {
        "question": "how many cyber-crime FIRs were registered in Bengaluru Urban in 2026?",
        "sql": "SELECT COUNT(*) AS cyber_frs_2026 FROM CaseMaster cm JOIN Unit u ON u.UnitID=cm.PoliceStationID JOIN District d ON d.DistrictID=u.DistrictID JOIN CrimeHead ch ON ch.CrimeHeadID=cm.CrimeMajorHeadID WHERE ch.CrimeGroupName='Cyber Crimes' AND d.DistrictName='Bengaluru Urban' AND strftime('%Y', cm.CrimeRegisteredDate)='2026'",
        "explanation": "Cyber-crime FIR count for Bengaluru Urban in 2026",
    },
    {
        "question": "list arrests made outside Karnataka in the last 6 months",
        "sql": "SELECT ars.ArrestSurrenderID, ars.ArrestSurrenderDate, s.StateName, d.DistrictName, a.AccusedName FROM ArrestSurrender ars JOIN State s ON s.StateID=ars.ArrestSurrenderStateId LEFT JOIN District d ON d.DistrictID=ars.ArrestSurrenderDistrictId LEFT JOIN Accused a ON a.AccusedMasterID=ars.AccusedMasterID WHERE ars.ArrestSurrenderStateId!=29 AND ars.ArrestSurrenderDate >= date('now','-6 months') ORDER BY ars.ArrestSurrenderDate DESC LIMIT 100",
        "explanation": "Recent cross-border arrests",
    },
    {
        "question": "police station chargesheet rate above 60%",
        "sql": "SELECT u.UnitName, COUNT(cm.CaseMasterID) AS total, SUM(CASE WHEN cs.cstype='A' THEN 1 ELSE 0 END) AS chargesheeted, ROUND(100.0*SUM(CASE WHEN cs.cstype='A' THEN 1 ELSE 0 END)/COUNT(cm.CaseMasterID),1) AS pct FROM CaseMaster cm JOIN Unit u ON u.UnitID=cm.PoliceStationID LEFT JOIN ChargesheetDetails cs ON cs.CaseMasterID=cm.CaseMasterID GROUP BY u.UnitID HAVING total>=50 AND pct>=60 ORDER BY pct DESC LIMIT 40",
        "explanation": "Stations with chargesheet rate ≥ 60% (min 50 cases)",
    },
    {
        "question": "average age of accused for narcotic offences",
        "sql": "SELECT AVG(a.AgeYear) AS avg_age FROM Accused a JOIN CaseMaster cm ON cm.CaseMasterID=a.CaseMasterID JOIN CrimeHead ch ON ch.CrimeHeadID=cm.CrimeMajorHeadID WHERE ch.CrimeGroupName='Narcotic Offences'",
        "explanation": "Average accused age for narcotic offences",
    },
    {
        "question": "which crime sub-heads spiked in the last 90 days?",
        "sql": "SELECT sh.CrimeHeadName, SUM(CASE WHEN cm.CrimeRegisteredDate >= date('now','-90 days') THEN 1 ELSE 0 END) AS recent, SUM(CASE WHEN cm.CrimeRegisteredDate < date('now','-90 days') AND cm.CrimeRegisteredDate >= date('now','-180 days') THEN 1 ELSE 0 END) AS prior FROM CaseMaster cm JOIN CrimeSubHead sh ON sh.CrimeSubHeadID=cm.CrimeMinorHeadID GROUP BY sh.CrimeHeadName HAVING recent > prior ORDER BY (recent - prior) DESC LIMIT 20",
        "explanation": "Sub-heads with growth in the last 90 days",
    },
    {
        "question": "list investigating officers with the highest caseload",
        "sql": "SELECT e.EmployeeID, e.FirstName || ' ' || e.LastName AS io_name, u.UnitName, COUNT(cm.CaseMasterID) AS caseload FROM Employee e JOIN CaseMaster cm ON cm.PolicePersonID=e.EmployeeID JOIN Unit u ON u.UnitID=e.UnitID JOIN Designation d ON d.DesignationID=e.DesignationID WHERE d.DesignationName='Investigating Officer' GROUP BY e.EmployeeID ORDER BY caseload DESC LIMIT 30",
        "explanation": "IO caseload leaderboard",
    },
    {
        "question": "act-section usage frequency",
        "sql": "SELECT asa.ActID, asa.SectionID, s.SectionDescription, COUNT(*) AS usages FROM ActSectionAssociation asa LEFT JOIN Section s ON s.ActCode=asa.ActID AND s.SectionCode=asa.SectionID GROUP BY asa.ActID, asa.SectionID ORDER BY usages DESC LIMIT 30",
        "explanation": "Most-cited act/section pairs",
    },
    {
        "question": "cases where the victim is a police officer",
        "sql": "SELECT cm.CrimeNo, cm.CrimeRegisteredDate, sh.CrimeHeadName, u.UnitName, v.VictimName FROM Victim v JOIN CaseMaster cm ON cm.CaseMasterID=v.CaseMasterID JOIN CrimeSubHead sh ON sh.CrimeSubHeadID=cm.CrimeMinorHeadID JOIN Unit u ON u.UnitID=cm.PoliceStationID WHERE v.VictimPolice=1 ORDER BY cm.CrimeRegisteredDate DESC LIMIT 50",
        "explanation": "Cases where a victim is police personnel",
    },
]


def build_training_set(out_path: Path, schema_text: str, extra_path: Path | None) -> int:
    data = list(CURATED_QA)
    if extra_path and extra_path.exists():
        with extra_path.open() as f:
            for line in f:
                if line.strip():
                    data.append(json.loads(line))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for row in data:
            record = {
                "instruction": SYSTEM_PROMPT_TMPL.format(schema=schema_text),
                "input": row["question"],
                "output": json.dumps(
                    {"sql": row["sql"], "explanation": row.get("explanation", "")}
                ),
            }
            f.write(json.dumps(record) + "\n")
    return len(data)


def _extract_schema() -> str:
    src = SCHEMA_SUMMARY.read_text()
    # Pull the SCHEMA_SUMMARY constant out of backend/llm.py so we don't have
    # to duplicate it here.
    m_start = src.find('SCHEMA_SUMMARY = """\\\n')
    m_end   = src.find('"""', m_start + 20)
    return src[m_start + 20 : m_end]


def run_training(base_model: str, dataset_path: Path, out_dir: Path,
                 epochs: int, batch_size: int, lora_r: int, learning_rate: float) -> None:
    try:
        import torch
        from datasets import load_dataset
        from peft import LoraConfig, get_peft_model
        from transformers import (
            AutoModelForCausalLM, AutoTokenizer,
            DataCollatorForLanguageModeling, Trainer, TrainingArguments,
            BitsAndBytesConfig,
        )
    except ImportError as e:
        print(f"[skip] transformers/peft/torch not available: {e}", file=sys.stderr)
        print("Install them (torch, transformers, peft, datasets, accelerate, "
              "bitsandbytes) and re-run to actually train.")
        return

    print(f"Loading base model {base_model}…")
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16)
    tok = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        base_model, quantization_config=bnb, device_map="auto", trust_remote_code=True,
    )
    peft_cfg = LoraConfig(
        r=lora_r, lora_alpha=lora_r * 2, lora_dropout=0.05, bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"], task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, peft_cfg)
    model.print_trainable_parameters()

    ds = load_dataset("json", data_files=str(dataset_path))["train"]

    def format_example(ex):
        prompt = f"{ex['instruction']}\n\nQuestion: {ex['input']}\n\nJSON: "
        text = prompt + ex["output"] + tok.eos_token
        toks = tok(text, truncation=True, max_length=1500, padding="max_length")
        toks["labels"] = toks["input_ids"].copy()
        return toks

    ds = ds.map(format_example, remove_columns=ds.column_names)
    args = TrainingArguments(
        output_dir=str(out_dir),
        per_device_train_batch_size=batch_size,
        num_train_epochs=epochs,
        learning_rate=learning_rate,
        warmup_ratio=0.1,
        logging_steps=1,
        save_strategy="epoch",
        bf16=True,
        gradient_accumulation_steps=4,
    )
    trainer = Trainer(
        model=model, args=args, train_dataset=ds,
        data_collator=DataCollatorForLanguageModeling(tok, mlm=False),
    )
    trainer.train()
    model.save_pretrained(str(out_dir))
    tok.save_pretrained(str(out_dir))
    print(f"LoRA adapter saved to {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="microsoft/phi-3-mini-4k-instruct")
    ap.add_argument("--out", default="models/ksp-sql-adapter")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--dataset", default="data/qa_train.jsonl")
    ap.add_argument("--extra-qa", default="data/qa.jsonl", help="Additional Q/SQL pairs (optional)")
    ap.add_argument("--prepare-only", action="store_true",
                    help="Only write the training JSONL; skip fine-tuning")
    args = ap.parse_args()

    schema = _extract_schema()
    ds_path = Path(args.dataset)
    n = build_training_set(ds_path, schema, Path(args.extra_qa) if args.extra_qa else None)
    print(f"Wrote {n} training examples to {ds_path}")
    if args.prepare_only:
        return
    run_training(args.base, ds_path, Path(args.out), args.epochs, args.batch_size, args.lora_r, args.lr)


if __name__ == "__main__":
    main()
