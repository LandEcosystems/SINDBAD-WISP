# Restore an omitted legacy A100 qualification receipt

The Agadir lane was created without the existing `mixed_batch_A100.json`
qualification. Its GPU worker therefore tried to reconstruct the original
eight-region qualification, although only Agadir inputs were available in that
lane. The worker returned successfully without claiming a month, and the
controller submitted it again. A Slurm `COMPLETED` state did not indicate that
prediction had occurred.

`recover_legacy_gpu_gate.py` restores the original, already-passed qualification
receipt after checking:

- the receipt SHA256 supplied by the operator;
- complete equality of the parent and lane scientific deployment manifests;
- the frozen scientific identity, eight qualification members, and eight
  batch comparisons and baseline-repeat records;
- the same A100 hardware and matching model and package identity in an actual
  lane GPU environment report.

The target is published atomically with its original bytes. A different existing
target is never overwritten. The separate recovery journal preserves source,
target, hashes, original qualification time, and the environment evidence.
Restarting after a partial operational recovery is supported. No prediction,
input, frozen numerical code, or queue state is changed by this utility.

Run without `--apply` first. Supply `--parent`, `--lane`, `--audit`, and the
known `--receipt-sha256`; `--apply` performs the verified publication.

The 2026-11-06 repair was deployed under
`wisp_eos_fire_episodes_batch1_v2/operations/legacy_gpu_gate_v1/`.
Its source receipt SHA256 was
`418f6acfb5fea052ae043b2f2dec4edb7c4af6839cb96e476d48cf9506d40249`.
The publication audit is `recovery.json` in that directory.
