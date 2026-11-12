"""Save the 3 new input payloads into dedicated folders under test/documents/."""
import json
from pathlib import Path

BASE_DIR = Path(r"c:\accorder ai\Applications\accorder-ai-backend\test\documents")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# Document 1: CoreWeave & Microsoft MSA
doc1_dir = BASE_DIR / "coreweave_microsoft_msa"
doc1_dir.mkdir(parents=True, exist_ok=True)

# Document 2: Brightstone & Crestview MSA
doc2_dir = BASE_DIR / "brightstone_crestview_msa"
doc2_dir.mkdir(parents=True, exist_ok=True)

# Document 3: Epit & XYZ Agreement
doc3_dir = BASE_DIR / "epit_xyz_spec_agreement"
doc3_dir.mkdir(parents=True, exist_ok=True)

print("Created folder structure under", BASE_DIR)

