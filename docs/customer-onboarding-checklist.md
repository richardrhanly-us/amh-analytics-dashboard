# Customer onboarding checklist (Collector 1.0.14)

**Internal only.** For SortView staff onboarding a new library, from provisioning to the first data. Do not send this
to customers: they get `docs/collector-1.0.14-customer-installation-guide.md`.

This checklist replaces `docs/SortView_Internal_Customer_Provisioning_Checklist.docx` (assisted 1.0.2 onboarding with a
hand-delivered API token and PowerShell scripts). That file is kept as a historical record only. None of its manual
token, ID or PowerShell steps apply to 1.0.14: SortView Setup enrolls the computer with a one-time code, and no API
token, operational ID or command is ever given to a customer.

The customer-facing support contact is **sortviewsuper@gmail.com**; the customer guide gives it to the library.

Every step below is done in SortView administration (`https://admin.sortviewapp.com`) unless it says otherwise. Do the
steps in order: each one depends on the one before.

---

## Before you start

- [ ] The library is approved to start (signed pilot agreement, payment or approved internal start).
- [ ] You have, from the library: its name and primary branch exactly as they should appear, the **first owner's**
      name and email address, and the name of the person who will install (library staff or IT technician).
- [ ] The installer will have a Windows administrator account on the computer that receives the Tech Logic files, and
      that computer meets the requirements in section 2 of the customer guide.
- [ ] An installation session is booked, with you available for its whole length.

## 1. Provision the library

- [ ] **Provision a library**: enter the library name, its slug, the primary branch name and slug, and the plan.
- [ ] Before submitting, check the name and slug against what the library gave you. A slug is permanent and is never
      rewritten.
- [ ] On the library page that opens, check **Operational identity** says **Assigned**. If it does not, select
      **Assign operational identity**.

## 2. Collector time zone and routing

- [ ] From the library page, open **Collector configuration**.
- [ ] Set the **Collector time zone** to the library's own time zone. Setup does not compare it with the Windows time
      zone on the library's computer, so it must be right here.
- [ ] Review **Internal Routing** with the library: these lists become the Collector's classification rules at
      enrollment. Save them with **Save Internal Routing**.
- [ ] Tick **I have reviewed this library's routing configuration** and select **Save collector settings**. A Contract 2
      installation cannot be added until this is done.

## 3. Assign the first owner

- [ ] **Tenant check:** the page heading and breadcrumb name **this** library. The first owner gets full access to it.
- [ ] In **First owner**, enter the owner's email address, type it again, and add their name. Select **Review**.
- [ ] **Email check:** on **Confirm the first owner**, read the address back to the library (by phone or in the
      session, not from your own notes) and confirm the library name shown. Then select **Assign first owner**.
- [ ] The section now says **This library has an owner**.

Notes:

- This is possible only while the library has **no** owner. Every other person is added by the library's own owners
  on the **Members** page of the SortView app.
- If the address already has a SortView account, that account is used as it is, with its existing password.
- A platform administrator's account cannot be made an owner.

## 4. Tell the owner how to get in

- [ ] Tell the owner which email address was used, and that SortView does **not** send a password or an invitation.
- [ ] Give them section 8 of the customer guide: go to `https://app.sortviewapp.com`, choose **Forgot password?**,
      and use the emailed link. The link works once and expires after 30 minutes.
- [ ] Confirm with the owner that the reset email arrived and that they could sign in. If it did not arrive, check
      their junk folder, then escalate to engineering. Do not set or send a password yourself.

## 5. Add the Collector installation

- [ ] **Tenant check:** open **Installations** from **this** library's page.
- [ ] Under **Add an installation**, give it a name the library will recognize (for example, the computer's purpose),
      keep **Contract 2 (current)** selected, and select **Add installation**.
- [ ] The new installation shows as **Provisioning** with its number (Installation #N). Note the number for step 9.

Do not choose Contract 1 for a new library.

## 6. Supply the installer package and its fingerprint

The approved package is the **G2** SortView Setup package (the GUI with recovery guidance and Copy details), built from
commit `bef28bc` with the canonical Collector 1.0.14 bundle:

| | |
|---|---|
| File | `SortView-Setup-1.0.14.zip` |
| Approved ZIP SHA-256 | `6f91da7da2253a4cdef85e4fa6f25789dd8a48a932bc4ae884eac1465ece3193` |

Do not use the earlier G3 package (`e932200f…`, built from `de858df`); it predates the recovery guidance.

- [ ] Before sending, compute the SHA-256 of the **exact ZIP file you are about to send** and confirm it equals the
      approved fingerprint above. If it differs, do not send it; find the approved artifact.
- [ ] Send the ZIP and the fingerprint by **separate** channels, so one message cannot be swapped for both.
- [ ] Ask the installer to check the fingerprint of the file they received before opening it (customer guide,
      section 3).
- [ ] Remind them that this is a **supervised-pilot** package, not a publicly signed release: it is not code-signed,
      its first pages show "Test build" notices, and Windows might warn about it.

## 7. Issue the enrollment code during the session

Issue the code **only when the installer is at the computer, has extracted the package and is working through
SortView Setup**. It expires 30 minutes after it is issued.

- [ ] **Tenant check, before issuing:** the page heading and breadcrumb name this library, and the installation is the
      Contract 2 installation from step 5. A code enrolls the computer into the library it was issued for: a code from
      the wrong library sends that library's data to the wrong place.
- [ ] Select **Issue enrollment code** on that installation. The code is shown **once**: copy it before leaving the
      page.
- [ ] Give the code to the installer by voice or another direct channel. Never put it in email, chat, tickets,
      screenshots or notes.
- [ ] Select **Done** once the installer has it.

If the code expires before it is used, issue a new one; the earlier one stops working. **But do not issue a new code
after a failed enrollment whose result is unknown** (no response from SortView, or an error from SortView): the
enrollment may already exist, and a Contract 2 enrollment cannot be repeated with a new code. Escalate to engineering
instead.

## 8. Assist with the installation

The installer follows section 5 of the customer guide. Be ready to:

- [ ] confirm the Tech Logic file locations (default `C:\TLCFinalDlls\Checkins.txt`, `Rejects.txt`, `ACS Log.txt`);
- [ ] confirm the installation number shown in the final result matches Installation #N from step 5;
- [ ] if setup stops, ask for the text from **Copy details** and follow **What to do now** (customer guide, section
      7). **Contact SortView support** results are engineering's to look at before anyone retries.

## 9. Verify

- [ ] **Installations:** the installation's status is **Active**. It becomes Active when the Collector first reports
      in, after its first scheduled run.
- [ ] **Library page, Sorters:** after the sorter has recorded activity and the Collector has run, **Onboarding** is
      **Ready** and **Pipeline** is **OK**.
- [ ] The owner can see the sorter's figures in the SortView app (no longer **Waiting for first data**).

The Collector runs about every 15 minutes (its first run is at the next trigger after installation), and it sends
only activity the sorter recorded after it was installed. No sorter activity means no data and **Waiting for first
data**, which is not a fault.

## 10. Record the onboarding

Record these in the library's onboarding record (outside this repository; never store codes, passwords or tokens):

- [ ] Library and slug; installation number; date and time of installation.
- [ ] Who installed it, and who from SortView assisted.
- [ ] Time of the first data, and the date onboarding was confirmed Ready.
- [ ] Any stop during setup: the **Copy details** text, what was done, and the outcome.
- [ ] Any follow-up owed to the library.

## Stop and escalate if

- the library name or slug on screen is not the library you are onboarding;
- the owner's email address could not be confirmed with the library;
- the package fingerprint does not match;
- SortView Setup reports **Contact SortView support**, or an enrollment result is unknown;
- sorter activity since the installation has been **confirmed with the library**, and it is still not reflected
  (installation not **Active**, onboarding not **Ready**) after the Collector's next few scheduled runs.
