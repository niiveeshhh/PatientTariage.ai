"""
FHIR R4 adapter - integration tier L3, DEMONSTRATED not connected.

Blueprint 6.10/16.4: India's ABDM ecosystem standardises on FHIR R4 through the
NRCeS implementation guide [S16].  This adapter ingests a SYNTHETIC ABDM-shaped
bundle (Patient, Encounter, Observation, Condition) and normalises it into the same
internal Patient object the native path produces.

The point of the module is the EQUIVALENCE TEST: the same patient expressed natively
and as a FHIR bundle must produce BYTE-IDENTICAL recommendations.  That converts
"we could integrate" from an assertion into a demonstrated property.

NO HOSPITAL CONNECTION IS ATTEMPTED OR CLAIMED.  100% synthetic bundles.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.adapters.base import AdapterError
from app.core.models import (
    AgeSource, ArrivalMode, CommunicationBarrier, IdentityLink, MatchState,
    MissingReason, Observation, Patient, PregnancyStatus, Provenance, Quality, absent,
)

# LOINC codes profiled by the ABDM Wellness Record for vitals [S16].
LOINC_MAP: Dict[str, str] = {
    "8867-4": "heart_rate",
    "9279-1": "respiratory_rate",
    "59408-5": "spo2",
    "2708-6": "spo2",
    "8480-6": "systolic_bp",
    "8462-4": "diastolic_bp",
    "8310-5": "temperature_c",
    "8478-0": "mean_arterial_pressure",
    "9269-2": "gcs",
    "72514-3": "pain_score",
    "2339-0": "blood_glucose_mgdl",
    "85353-1": "vital_signs_panel",
}

# SNOMED-ish codes we accept for the structured quick-look.
CODE_MAP: Dict[str, str] = {
    "248234008": "consciousness_acvpu",
    "271823003": "capillary_refill_seconds",
    "248546002": "work_of_breathing",
}

ARRIVAL_MODE_MAP = {
    "EMD": ArrivalMode.AMBULANCE, "AMB": ArrivalMode.AMBULANCE,
    "WALK": ArrivalMode.WALK_IN, "RTE": ArrivalMode.REFERRED,
    "TRN": ArrivalMode.INTER_FACILITY,
}

_DEVICE_FIELDS = {"heart_rate", "respiratory_rate", "spo2", "systolic_bp",
                  "diastolic_bp", "temperature_c", "blood_glucose_mgdl"}
_OBS_FIELDS = {"consciousness_acvpu", "capillary_refill_seconds", "work_of_breathing",
               "gcs"}


class FhirR4Adapter:
    tier = "L3"
    name = "fhir_r4_abdm"

    # ------------------------------------------------------------------
    def to_patient(self, bundle: Dict[str, Any], now_min: float,
                   patient_ref: Optional[str] = None) -> Patient:
        if bundle.get("resourceType") != "Bundle":
            raise AdapterError("not a FHIR Bundle")
        entries = bundle.get("entry") or []
        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for e in entries:
            res = e.get("resource") or {}
            by_type.setdefault(res.get("resourceType", "?"), []).append(res)

        pat_res = (by_type.get("Patient") or [None])[0]
        if pat_res is None:
            raise AdapterError("bundle contains no Patient resource")
        enc_res = (by_type.get("Encounter") or [{}])[0]

        patient = Patient(
            patient_ref=patient_ref or self._ref(pat_res),
            arrival_timestamp_min=now_min,
            chair=self._chair(enc_res),
            age_days=self._age_days(pat_res),
            age_source=self._age_source(pat_res),
            age_estimated=self._age_source(pat_res) == AgeSource.ESTIMATED,
            sex=self._sex(pat_res),
            arrival_mode=self._arrival_mode(enc_res),
            stated_chief_complaint=self._complaint(enc_res, by_type),
            record_id=self._record_id(pat_res),
        )

        identity = self._identity(pat_res)
        patient.identity = identity

        for cond in by_type.get("Condition", []):
            name = self._condition_name(cond)
            if name:
                patient.known_conditions.append(name)
        patient.known_conditions = sorted(set(patient.known_conditions))

        for obs in by_type.get("Observation", []):
            self._ingest_observation(patient, obs, now_min)

        self._apply_extensions(patient, pat_res, enc_res, now_min)
        return patient

    # ------------------------------------------------------------------
    def _ref(self, res: Dict[str, Any]) -> str:
        for ident in res.get("identifier", []):
            if ident.get("value"):
                return f"FHIR-{ident['value']}"
        return f"FHIR-{res.get('id', 'unknown')}"

    def _record_id(self, res: Dict[str, Any]) -> Optional[str]:
        for ident in res.get("identifier", []):
            system = (ident.get("system") or "").lower()
            if "abha" in system or "abdm" in system or "mrn" in system:
                return ident.get("value")
        return res.get("id")

    def _age_days(self, res: Dict[str, Any]) -> Optional[int]:
        ext = self._extension(res, "ageInDays")
        if ext is not None:
            return int(ext)
        ext_years = self._extension(res, "ageInYears")
        if ext_years is not None:
            return int(float(ext_years) * 365)
        return None

    def _age_source(self, res: Dict[str, Any]) -> AgeSource:
        v = self._extension(res, "ageSource")
        try:
            return AgeSource(v) if v else AgeSource.DOCUMENT
        except ValueError:
            return AgeSource.DOCUMENT

    def _sex(self, res: Dict[str, Any]) -> str:
        return {"male": "M", "female": "F", "other": "other",
                "unknown": "unknown"}.get(res.get("gender", "unknown"), "unknown")

    def _arrival_mode(self, enc: Dict[str, Any]) -> ArrivalMode:
        for h in enc.get("hospitalization", {}).get("admitSource", {}).get("coding", []):
            m = ARRIVAL_MODE_MAP.get(h.get("code", ""))
            if m:
                return m
        cls = (enc.get("class") or {}).get("code", "")
        return ARRIVAL_MODE_MAP.get(cls, ArrivalMode.WALK_IN)

    def _chair(self, enc: Dict[str, Any]) -> str:
        for loc in enc.get("location", []):
            d = (loc.get("location") or {}).get("display")
            if d:
                return d
        return ""

    def _complaint(self, enc: Dict[str, Any], by_type: Dict[str, List]) -> str:
        for r in enc.get("reasonCode", []):
            if r.get("text"):
                return r["text"]
            for c in r.get("coding", []):
                if c.get("display"):
                    return c["display"]
        for cond in by_type.get("Condition", []):
            cat = str(cond.get("category", ""))
            if "chief-complaint" in cat or "encounter-diagnosis" in cat:
                code = cond.get("code", {})
                if code.get("text"):
                    return code["text"]
        return ""

    def _condition_name(self, cond: Dict[str, Any]) -> Optional[str]:
        code = cond.get("code", {})
        text = code.get("text")
        if text:
            return text.lower().replace(" ", "_")
        for c in code.get("coding", []):
            if c.get("display"):
                return c["display"].lower().replace(" ", "_")
        return None

    def _identity(self, res: Dict[str, Any]) -> IdentityLink:
        conf = self._extension(res, "identityConfidence")
        matched = self._extension(res, "matchedFields")
        rid = self._record_id(res)
        if conf is None:
            return IdentityLink(match_state=MatchState.UNMATCHED)
        confidence = float(conf)
        state = (MatchState.MATCHED if confidence >= 0.90
                 else MatchState.PROVISIONAL if confidence >= 0.50
                 else MatchState.UNMATCHED)
        return IdentityLink(
            match_state=state, identity_confidence=confidence,
            matched_fields=(matched.split(",") if isinstance(matched, str) else []),
            candidate_record_ids=[rid] if rid else [],
        )

    def _extension(self, res: Dict[str, Any], key: str) -> Any:
        for ext in res.get("extension", []):
            url = ext.get("url", "")
            if url.rsplit("/", 1)[-1] == key or url == key:
                for k in ("valueInteger", "valueDecimal", "valueString",
                          "valueBoolean"):
                    if k in ext:
                        return ext[k]
        return None

    # ------------------------------------------------------------------
    def _ingest_observation(self, patient: Patient, obs: Dict[str, Any],
                            now_min: float) -> None:
        for component in self._components(obs):
            field_name, value, unit = component
            if field_name is None:
                continue

            status = obs.get("status", "final")
            if status in ("entered-in-error", "cancelled"):
                # Blueprint 8.7: absence carries a REASON. Never silently dropped.
                patient.set_observation(
                    absent(field_name, MissingReason.DEVICE_FAILED, now_min))
                continue

            if value is None:
                reason = self._data_absent_reason(obs)
                patient.set_observation(absent(field_name, reason, now_min))
                continue

            if field_name == "temperature_c" and unit in ("[degF]", "degF", "F"):
                value = round((float(value) - 32.0) * 5.0 / 9.0, 1)

            prov = (Provenance.DEV if field_name in _DEVICE_FIELDS
                    else Provenance.OBS if field_name in _OBS_FIELDS
                    else Provenance.PT)
            quality = Quality.CLEAN if status == "final" else Quality.SUSPECT
            age = self._age_minutes(obs)

            patient.set_observation(Observation(
                field_name=field_name,
                value=int(value) if field_name in {
                    "heart_rate", "respiratory_rate", "spo2", "systolic_bp",
                    "diastolic_bp", "pain_score", "gcs", "blood_glucose_mgdl"}
                and isinstance(value, (int, float)) else value,
                provenance=prov, timestamp_min=now_min - age, quality=quality,
                source_confidence=0.95 if prov == Provenance.DEV else 0.9,
            ))

    def _components(self, obs: Dict[str, Any]):
        """A vital-signs panel carries components; a simple observation does not."""
        out = []
        if obs.get("component"):
            for comp in obs["component"]:
                out.append(self._one(comp))
        else:
            out.append(self._one(obs))
        return out

    def _one(self, node: Dict[str, Any]):
        code = node.get("code", {})
        field_name = None
        for c in code.get("coding", []):
            key = c.get("code")
            field_name = LOINC_MAP.get(key) or CODE_MAP.get(key)
            if field_name:
                break
        if field_name is None and code.get("text"):
            field_name = code["text"].strip().lower().replace(" ", "_")
        vq = node.get("valueQuantity") or {}
        value = vq.get("value")
        unit = vq.get("code") or vq.get("unit")
        if value is None:
            cc = node.get("valueCodeableConcept") or {}
            value = cc.get("text") or next(
                (c.get("code") for c in cc.get("coding", []) if c.get("code")), None)
        if value is None and "valueString" in node:
            value = node["valueString"]
        if value is None and "valueBoolean" in node:
            value = node["valueBoolean"]
        return field_name, value, unit

    def _data_absent_reason(self, obs: Dict[str, Any]) -> MissingReason:
        dar = obs.get("dataAbsentReason", {})
        code = next((c.get("code") for c in dar.get("coding", [])), None) or dar.get("text")
        return {
            "not-performed": MissingReason.NOT_YET_TAKEN,
            "not-asked": MissingReason.NOT_YET_TAKEN,
            "asked-declined": MissingReason.REFUSED,
            "error": MissingReason.DEVICE_FAILED,
            "not-applicable": MissingReason.NOT_APPLICABLE,
            "unknown": MissingReason.UNKNOWN,
        }.get(code, MissingReason.UNKNOWN)

    def _age_minutes(self, obs: Dict[str, Any]) -> float:
        for ext in obs.get("extension", []):
            if ext.get("url", "").endswith("ageMinutes"):
                return float(ext.get("valueDecimal", 0.0))
        return 0.0

    def _apply_extensions(self, patient: Patient, pat_res: Dict[str, Any],
                          enc_res: Dict[str, Any], now_min: float) -> None:
        preg = self._extension(pat_res, "pregnancyStatus")
        if preg:
            try:
                patient.pregnancy_status = PregnancyStatus(preg)
            except ValueError:
                patient.pregnancy_status = PregnancyStatus.UNKNOWN
        barrier = self._extension(enc_res, "communicationBarrier")
        if barrier:
            patient.communication_barrier = True
            try:
                patient.communication_barrier_kind = CommunicationBarrier(barrier)
            except ValueError:
                patient.communication_barrier_kind = CommunicationBarrier.LANGUAGE
            patient.self_report_channel_available = False
        baseline = self._extension(pat_res, "baselineSystolicBp")
        if baseline is not None:
            patient.baseline_systolic_bp = int(baseline)
        rate = self._extension(pat_res, "rateControlMedication")
        if rate is not None:
            patient.rate_control_medication = bool(rate)
        oriented = self._extension(pat_res, "baselineOriented")
        if oriented is not None:
            patient.baseline_oriented = bool(oriented)


# ---------------------------------------------------------------------------
# Export: build a synthetic ABDM-shaped bundle FROM an internal patient, so the
# equivalence test has something to round-trip.
# ---------------------------------------------------------------------------

REVERSE_LOINC = {
    "heart_rate": ("8867-4", "Heart rate", "/min"),
    "respiratory_rate": ("9279-1", "Respiratory rate", "/min"),
    "spo2": ("59408-5", "Oxygen saturation", "%"),
    "systolic_bp": ("8480-6", "Systolic blood pressure", "mm[Hg]"),
    "diastolic_bp": ("8462-4", "Diastolic blood pressure", "mm[Hg]"),
    "temperature_c": ("8310-5", "Body temperature", "Cel"),
    "pain_score": ("72514-3", "Pain severity", "{score}"),
    "gcs": ("9269-2", "Glasgow coma score total", "{score}"),
    "blood_glucose_mgdl": ("2339-0", "Glucose Bld-mCnc", "mg/dL"),
}
REVERSE_CODE = {
    "consciousness_acvpu": ("248234008", "Level of consciousness"),
    "capillary_refill_seconds": ("271823003", "Capillary refill time"),
    "work_of_breathing": ("248546002", "Work of breathing"),
}

_ABSENT_TO_FHIR = {
    MissingReason.NOT_YET_TAKEN: "not-performed",
    MissingReason.REFUSED: "asked-declined",
    MissingReason.DEVICE_FAILED: "error",
    MissingReason.NOT_APPLICABLE: "not-applicable",
    MissingReason.NOT_OBTAINABLE: "unknown",
    MissingReason.UNKNOWN: "unknown",
}


def to_bundle(patient: Patient, now_min: float) -> Dict[str, Any]:
    """Emit a synthetic ABDM-shaped FHIR R4 bundle for this patient."""
    ext: List[Dict[str, Any]] = []
    if patient.age_days is not None:
        ext.append({"url": "http://patienttriage.ai/fhir/ageInDays",
                    "valueInteger": int(patient.age_days)})
    ext.append({"url": "http://patienttriage.ai/fhir/ageSource",
                "valueString": patient.age_source.value})
    if patient.identity.identity_confidence:
        ext.append({"url": "http://patienttriage.ai/fhir/identityConfidence",
                    "valueDecimal": patient.identity.identity_confidence})
        ext.append({"url": "http://patienttriage.ai/fhir/matchedFields",
                    "valueString": ",".join(patient.identity.matched_fields)})
    if patient.pregnancy_status:
        ext.append({"url": "http://patienttriage.ai/fhir/pregnancyStatus",
                    "valueString": patient.pregnancy_status.value})
    if patient.baseline_systolic_bp is not None:
        ext.append({"url": "http://patienttriage.ai/fhir/baselineSystolicBp",
                    "valueInteger": int(patient.baseline_systolic_bp)})
    if patient.rate_control_medication is not None:
        ext.append({"url": "http://patienttriage.ai/fhir/rateControlMedication",
                    "valueBoolean": bool(patient.rate_control_medication)})
    if patient.baseline_oriented is not None:
        ext.append({"url": "http://patienttriage.ai/fhir/baselineOriented",
                    "valueBoolean": bool(patient.baseline_oriented)})

    gender = {"M": "male", "F": "female"}.get(patient.sex, "unknown")
    entries: List[Dict[str, Any]] = [
        {"resource": {
            "resourceType": "Patient",
            "id": patient.patient_ref,
            "identifier": [{"system": "https://healthid.abdm.gov.in/",
                            "value": patient.record_id or patient.patient_ref}],
            "gender": gender,
            "extension": ext,
        }},
    ]

    enc_ext = []
    if patient.communication_barrier:
        enc_ext.append({"url": "http://patienttriage.ai/fhir/communicationBarrier",
                        "valueString": patient.communication_barrier_kind.value})
    entries.append({"resource": {
        "resourceType": "Encounter",
        "id": f"enc-{patient.patient_ref}",
        "status": "in-progress",
        "class": {"code": {"ambulance": "EMD", "walk_in": "WALK",
                           "referred": "RTE",
                           "inter_facility_transfer": "TRN",
                           "police": "WALK"}.get(patient.arrival_mode.value, "WALK")},
        "reasonCode": [{"text": patient.stated_chief_complaint}],
        "location": ([{"location": {"display": patient.chair}}] if patient.chair else []),
        "extension": enc_ext,
    }})

    for cond in patient.known_conditions:
        entries.append({"resource": {
            "resourceType": "Condition",
            "id": f"cond-{cond}",
            "code": {"text": cond.replace("_", " ")},
        }})

    for name in sorted(patient.observations):
        obs = patient.observations[name]
        node: Dict[str, Any] = {
            "resourceType": "Observation",
            "id": f"obs-{name}",
            "status": "final",
            "extension": [{"url": "http://patienttriage.ai/fhir/ageMinutes",
                           "valueDecimal": round(obs.age_minutes(now_min), 4)}],
        }
        if name in REVERSE_LOINC:
            code, display, unit = REVERSE_LOINC[name]
            node["code"] = {"coding": [{"system": "http://loinc.org", "code": code,
                                        "display": display}]}
            if obs.value is None:
                node["dataAbsentReason"] = {"coding": [
                    {"code": _ABSENT_TO_FHIR.get(obs.missing_reason, "unknown")}]}
            else:
                node["valueQuantity"] = {"value": obs.value, "unit": unit, "code": unit}
        elif name in REVERSE_CODE:
            code, display = REVERSE_CODE[name]
            node["code"] = {"coding": [{"system": "http://snomed.info/sct",
                                        "code": code, "display": display}]}
            if obs.value is None:
                node["dataAbsentReason"] = {"coding": [
                    {"code": _ABSENT_TO_FHIR.get(obs.missing_reason, "unknown")}]}
            elif isinstance(obs.value, (int, float)):
                node["valueQuantity"] = {"value": obs.value, "unit": "s", "code": "s"}
            else:
                node["valueCodeableConcept"] = {"text": str(obs.value)}
        else:
            node["code"] = {"text": name}
            if obs.value is None:
                node["dataAbsentReason"] = {"coding": [
                    {"code": _ABSENT_TO_FHIR.get(obs.missing_reason, "unknown")}]}
            elif isinstance(obs.value, bool):
                node["valueBoolean"] = obs.value
            elif isinstance(obs.value, (int, float)):
                node["valueQuantity"] = {"value": obs.value}
            else:
                node["valueString"] = str(obs.value)
        entries.append({"resource": node})

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "meta": {"profile": ["https://nrces.in/ndhm/fhir/r4/StructureDefinition/Bundle"]},
        "_synthetic": True,
        "_note": ("100% SYNTHETIC. No real patient record. Shaped after the ABDM / "
                  "NRCeS FHIR R4 implementation guide [S16]. No hospital connection "
                  "is attempted or claimed."),
        "entry": entries,
    }
