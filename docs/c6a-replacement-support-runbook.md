# Replacing a Collector computer — support runbook

**Status:** reviewed against the C6a R3 administration interface and the server-side replacement flag, as implemented on 2026-11-12 (`docs/c6a-replacement-machine-design.md`, 14.11). The labels, buttons and messages quoted here are the interface's own. Replacement is still not qualified for customer use (R4, R5, D6).

**For:** SortView support staff who are platform administrators in SortView Administration.

**Scope:** Mode 2 replacement (fresh-identity recovery). The replacement computer gets a new identity and a new key. The old computer's credentials are retired. Stored data is kept. Mode 1, which keeps the old identity, does not exist.

**Customer use:** replacement has not been qualified for customer use. Use it only as decision D6 allows (supervised pilot, with support doing every step), or on test libraries.

---

## 1. Before you start

Check every item. If one fails, stop.

1. **The flag is on.** The backend has `SORTVIEW_MACHINE_REPLACEMENT_ENABLED=true`. If it is off, the Installations page says "Machine replacement is turned off on this server. A pending replacement can still be cancelled." and offers no **Replace this Collector computer** button. Only an operator can change it, by changing the DigitalOcean app's environment. Changing it redeploys the backend.
2. **The use is allowed** under D6 for this library.
3. **Replacement is the right fix.** It is not the right fix if the computer can resume setup or only lost its token (Section 2).
4. **The new computer is ready:** clean Windows, the unchanged SortView Setup and Collector 1.0.14 package (the G2 package), and access to the sorter's Tech Logic files.
5. **No active legacy unbound tokens at the branch.** On the Installations page, use **Show legacy unbound tokens**. Revoke any that remain, after confirming nothing still needs them. Authorization is refused while any is active.
6. **For a planned replacement, reduce the data gap.** Stop the sorter. Let the old computer finish one successful run. Confirm it on the sorter diagnostics page (*Last success*, *Last collector run*) before the new computer redeems its code.

### What the customer must be told

- **The old computer keeps working until the code is used.** Authorizing a replacement does not disable the original computer. Its token and key stop working only when the new computer's SortView Setup redeems the code successfully.
- **There may be a gap in collected data.** Mode 2 does not guarantee gap-free collection:
  - Records written after the old computer's last successful upload and before the new computer starts are not collected. The new computer starts at the end of each file.
  - A hold recorded before the replacement cannot be corrected by a later record.
  - Items near the switch may be counted twice.
- **Existing data is kept.** Reports still show everything stored before the replacement.

## 2. Choose the right fix

| What happened | Fix | Replace? |
|---|---|---|
| Setup stopped part-way, and running SortView Setup again offers **Resume setup** | Run SortView Setup again and choose **Resume setup** | No |
| Setup finished enrolling but could not store its token | **Issue recovery token** for that installation, store it with `tools\set-api-token.ps1`, then resume setup | No |
| Setup reported an **unknown result** and offered no Resume setup (the enrollment response was lost) | Replace that installation (Section 6) | Yes, with the lost-response confirmation |
| The computer failed, was lost or stolen, or is being retired | Replace it (Section 3) | Yes |
| The computer may be compromised | **Deactivate** it first, then replace it | Yes |

Never reuse a code that was already used, and never retire the old installation by hand. Redemption retires it.

## 3. Authorize the replacement

1. Open **Libraries → the library → Installations**.
2. On the card of the computer being replaced, choose **Replace this Collector computer**. The button appears only while the flag is on, only on an enrolled Contract 2 installation at the primary branch, and only while no other replacement is pending. The server checks every rule again: a button on the page is not permission.
3. Read the explanation at the top of the form. Then, under **Name for the replacement computer**, enter a name, for example "Main AMH Sorter (replacement)".
4. **If the old installation is Provisioning:** tick the confirmation that SortView Setup reported an unknown result and offered no Resume setup, and that you tried Resume setup and the recovery token first. Authorization is refused without it.
5. Choose **Confirm: Authorize replacement**. **Cancel** closes the form without changing anything.

**Result:**
- A new installation appears, Provisioning, labelled "Pending replacement for Installation #X".
- The old card reads "Being replaced by Installation #Y".
- A message at the top of the page confirms the authorization, and the keyboard focus moves to the new installation's card.
- Nothing about the old computer has changed. It keeps uploading.

**Audit record:** `admin_replacement_authorized`, naming you, both installations and the old installation's status. It also records the lost-response confirmation if you gave it.

If authorization is refused, the page says why and changes nothing (Section 9).

## 4. Issue the enrollment code

Do this when the new computer is ready, not before. Codes expire.

1. On the **pending replacement's** card, choose **Issue enrollment code**.
2. The code is shown **once**. Copy it, give it to whoever runs setup on the new computer, then choose **Done**.
   - Do not paste it into tickets, chat or email.
   - It cannot be shown again. If it is lost, issue a new one: the old one stops working.
3. Note the expiry time shown with it.

**Audit record:** `admin_enrollment_code_issued`. It records the code's row id, never the code.

## 5. Install and redeem on the new computer

1. On the new computer, run **SortView Setup** and enter the code.
2. When Setup accepts it, the replacement takes effect in one step. At that moment:
   - the old computer's token, installation and key are retired, so it is refused from then on;
   - the new installation gets its own key and token;
   - the branch's existing data and its Contract 2 cutover are unchanged.
3. Let Setup finish, including its connection checks.

**Audit record:** `collector_replacement_redeemed`, a system action. It names the administrator who authorized the replacement, both installations, the retired and new key ids, and the deactivated token ids. It never records a code, token or key secret.

**If Setup says the library is "not ready to be enrolled":** the code was **not** used, and nothing changed. Check Section 9, fix the cause, and run Setup again with the same code before it expires.

## 6. Verify before cleaning up

Do not touch the old computer until all of these hold:

1. **Installations page:**
   - the new installation is **Active**, with a recent *Last seen*, and reads "Replaces Installation #X";
   - the old installation is **Retired** and reads "Replaced by Installation #Y".
2. **Sorter diagnostics page** (from the library overview's *Sorters* table):
   - a recent *Last heartbeat* and *Last success* from the new computer;
   - *Uploaded check-in rows* increasing once the sorter is running again.
3. **Run the sorter** and confirm new activity appears.

If the new installation stays Provisioning, or sends heartbeats but uploads nothing while the sorter is running, stop. Investigate before any cleanup: the old computer is the only record of what it had not yet sent.

## 7. Clean up the old computer

Only after Section 6 passes:

1. On the old computer, disable the **SortView Collector** scheduled task.
2. Run `tools\uninstall.ps1`.
3. Note in the support ticket that the old computer was cleaned up, and when.

The old computer's local secret is useless to SortView once its key is retired. Cleanup stops it from retrying uploads that will always be refused.

## 8. Cancel a replacement that was not used

Use this when the replacement is abandoned before its code is redeemed: wrong computer, plans changed, or authorized by mistake.

1. On the pending replacement's card, choose **Cancel replacement**, read the warning, then choose **Confirm: Cancel replacement**. **Keep replacement** closes the warning without changing anything.
2. **Result:**
   - the pending installation is Retired;
   - its unused codes stop working;
   - the old computer is unaffected.

Cancellation works even while the replacement flag is off.

**Audit record:** `admin_replacement_cancelled`, with the number of codes revoked.

Once a code has been redeemed, the replacement cannot be cancelled. The page says "This installation is not a pending replacement." and shows the current state. To undo a redeemed replacement, replace again (Section 3; for a lost response, Section 9).

## 9. Lost responses, expired codes and refusals

### The enrollment response was lost

SortView Setup on the **new** computer reported an unknown result and offered no Resume setup. Its code is used, and the new installation now owns the branch's key, but the computer holds nothing.

1. Do not reuse the code. It will never work again.
2. On the new installation's card (Provisioning, code used), choose **Replace this Collector computer**.
3. Tick the lost-response confirmation, and authorize a further replacement.
4. Continue from Section 4. The chain is recorded: each installation names the one it replaced.

Before doing this, confirm the computer really cannot resume: run Setup again and look for **Resume setup**.

### The code expired

The pending card reads "No unused code. Issue one when the replacement computer is ready." Issue a new one (Section 4). The replacement itself does not expire.

### The flag is off

- No new replacement can be authorized, and no code can be issued for a pending one.
- SortView Setup reports "not ready to be enrolled" for an existing code, and the code is not used.
- Cancellation still works.
- Ask an operator whether the flag should be on.

### Another administrator acted at the same time

The page shows the refusal and refreshes. Read the new state before acting again.

### Refusals

| Message | Meaning and fix |
|---|---|
| A retired installation cannot be replaced | Choose the installation that is currently enrolled |
| Only a Contract 2 installation can be replaced | Contract 1 Collectors are not covered by replacement |
| Only an installation at the library's active primary branch can be replaced | Check the library's branch setup |
| Only the installation that owns its branch's only active ingest key can be replaced | The branch's keys need operator reconciliation first; escalate |
| This branch has no Contract 2 enrollment to replace | The branch was never enrolled on Contract 2; use an ordinary installation |
| This branch still has an active legacy unbound token | Revoke it (Section 1, item 5), then try again |
| This branch already has a pending replacement | Use it, or cancel it first |
| This installation is still Provisioning. Confirm that its enrollment response was lost … | Follow Section 2 first. Tick the confirmation only for a genuinely lost response |
| This installation is not a pending replacement | It was already redeemed or cancelled; refresh |
| Machine replacement is turned off on this server. A pending replacement can still be cancelled. | The flag is off (see above) |

## 10. What each step leaves behind

| Step | Installation changes | Audit event |
|---|---|---|
| Authorize | New Provisioning installation linked to the old one | `admin_replacement_authorized` |
| Issue a code | Earlier unused codes of the replacement revoked | `admin_enrollment_code_issued` |
| Redeem | Old installation, tokens and key retired; new key and token | `collector_replacement_redeemed` |
| Cancel | Pending replacement retired; its codes revoked | `admin_replacement_cancelled` |

The link from each replacement to the installation it replaced, who authorized it and when, is write-once: the database refuses any change to it.
