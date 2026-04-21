import json
import logging
import os
import smtplib
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Iterable
from zoneinfo import ZoneInfo

import oci
from oci.exceptions import ServiceError

TARGET_SHAPE = "VM.Standard.A1.Flex"
TARGET_OCPUS = 4
TARGET_MEMORY_GB = 24
MADRID_TZ = ZoneInfo("Europe/Madrid")


@dataclass
class CapacityResult:
    region: str
    availability_domain: str
    status: str
    available_count: int | None
    timestamp_utc: datetime
    diagnostic: str


@dataclass
class ScanContext:
    bootstrap_region: str
    timestamp_utc: datetime
    timestamp_madrid: datetime


@dataclass
class StackNotificationPlan:
    should_send: bool
    day_number: int | None
    deployed_at: datetime | None


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def send_email(subject: str, body: str) -> None:
    email_user = os.environ["EMAIL_USER"]
    email_password = os.environ["EMAIL_APP_PASSWORD"]
    email_to = os.environ["EMAIL_TO"]
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_user
    msg["To"] = email_to
    msg.set_content(body)

    context = ssl.create_default_context()

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(email_user, email_password)
        server.send_message(msg)


def build_oci_config(private_key_path: str) -> dict:
    return {
        "user": _get_required_env("OCI_USER_OCID"),
        "tenancy": _get_required_env("OCI_TENANCY_OCID"),
        "fingerprint": _get_required_env("OCI_FINGERPRINT"),
        "key_file": private_key_path,
        "region": _clean_env_value(os.environ.get("OCI_REGION", "eu-madrid-3")),
    }


def now_context(bootstrap_region: str) -> ScanContext:
    ts_utc = datetime.now(timezone.utc)
    return ScanContext(
        bootstrap_region=bootstrap_region,
        timestamp_utc=ts_utc,
        timestamp_madrid=ts_utc.astimezone(MADRID_TZ),
    )


def get_realm_regions(identity_client: oci.identity.IdentityClient) -> list[str]:
    regions = identity_client.list_regions().data
    region_names = sorted({region.name for region in regions if getattr(region, "name", None)})
    if not region_names:
        raise RuntimeError("OCI no devolvió regiones para el realm actual.")
    return region_names


def load_regions_from_catalog(catalog_path: str) -> list[str]:
    with open(catalog_path, "r", encoding="utf-8") as catalog_file:
        catalog = json.load(catalog_file)

    realms = catalog.get("realms", {})
    region_ids: list[str] = []

    for entries in realms.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            region_id = entry.get("region_identifier")
            if region_id:
                region_ids.append(region_id)

    deduped_region_ids = sorted(set(region_ids))
    if not deduped_region_ids:
        raise RuntimeError(f"No se encontraron region_identifier en {catalog_path}.")
    return deduped_region_ids


def list_region_ads(identity_client: oci.identity.IdentityClient, tenancy_ocid: str) -> list[str]:
    ads = identity_client.list_availability_domains(compartment_id=tenancy_ocid).data
    return [ad.name for ad in ads if getattr(ad, "name", None)]


def _status_line(result: CapacityResult) -> str:
    return (
        f"region={result.region} | ad={result.availability_domain} | status={result.status} "
        f"| available_count={result.available_count} | timestamp={result.timestamp_utc.isoformat()} "
        f"| diagnostic={result.diagnostic}"
    )


def create_capacity_payload(tenancy_ocid: str, ad_name: str):
    return oci.core.models.CreateComputeCapacityReportDetails(
        compartment_id=tenancy_ocid,
        availability_domain=ad_name,
        shape_availabilities=[
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=TARGET_SHAPE,
                instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                    ocpus=TARGET_OCPUS,
                    memory_in_gbs=TARGET_MEMORY_GB,
                ),
            )
        ],
    )


def scan_region(
    base_config: dict,
    tenancy_ocid: str,
    region: str,
    run_timestamp_utc: datetime,
) -> list[CapacityResult]:
    region_config = dict(base_config)
    region_config["region"] = region

    results: list[CapacityResult] = []

    try:
        identity_client = oci.identity.IdentityClient(region_config)
        compute_client = oci.core.ComputeClient(region_config)
        ads = list_region_ads(identity_client, tenancy_ocid)
    except ServiceError as exc:
        logging.warning(
            "No se puede consultar la región %s (code=%s, status=%s). Se continúa.",
            region,
            getattr(exc, "code", "UNKNOWN"),
            getattr(exc, "status", "UNKNOWN"),
        )
        return results

    if not ads:
        logging.warning("La región %s no devolvió Availability Domains. Se continúa.", region)
        return results

    for ad_name in ads:
        try:
            payload = create_capacity_payload(tenancy_ocid, ad_name)
            report = compute_client.create_compute_capacity_report(payload).data
            for item in report.shape_availabilities:
                status = getattr(item, "availability_status", "UNKNOWN")
                available_count = getattr(item, "available_count", None)
                diagnostic = (
                    getattr(item, "status_message", None)
                    or getattr(item, "message", None)
                    or ""
                )
                result = CapacityResult(
                    region=region,
                    availability_domain=ad_name,
                    status=status,
                    available_count=available_count,
                    timestamp_utc=run_timestamp_utc,
                    diagnostic=diagnostic,
                )
                results.append(result)
                logging.info(_status_line(result))
        except ServiceError as exc:
            diagnostic = (
                f"ServiceError(code={getattr(exc, 'code', 'UNKNOWN')}, "
                f"status={getattr(exc, 'status', 'UNKNOWN')})"
            )
            result = CapacityResult(
                region=region,
                availability_domain=ad_name,
                status="ERROR",
                available_count=None,
                timestamp_utc=run_timestamp_utc,
                diagnostic=diagnostic,
            )
            results.append(result)
            logging.warning(
                "Error no fatal consultando capacidad en región=%s, ad=%s: %s. Se continúa.",
                region,
                ad_name,
                diagnostic,
            )
        except Exception as exc:  # pylint: disable=broad-except
            result = CapacityResult(
                region=region,
                availability_domain=ad_name,
                status="ERROR",
                available_count=None,
                timestamp_utc=run_timestamp_utc,
                diagnostic=f"{type(exc).__name__}: {exc}",
            )
            results.append(result)
            logging.warning(
                "Error inesperado no fatal en región=%s, ad=%s: %s. Se continúa.",
                region,
                ad_name,
                exc,
            )

    return results


def has_capacity_hit(result: CapacityResult) -> bool:
    if result.status != "AVAILABLE":
        return False
    try:
        return int(result.available_count or 0) > 0
    except (TypeError, ValueError):
        return False


def format_hits_table(results: Iterable[CapacityResult]) -> str:
    header = "region | availability_domain | status | available_count | timestamp"
    sep = "-" * len(header)
    rows = [header, sep]
    for res in results:
        rows.append(
            f"{res.region} | {res.availability_domain} | {res.status} | "
            f"{res.available_count} | {res.timestamp_utc.isoformat()}"
        )
    return "\n".join(rows)


def repo_name_from_env() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "(desconocido)")


def _normalize_ocid(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    return stripped or None


def _clean_env_value(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _get_required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise KeyError(name)

    cleaned = _clean_env_value(value)
    if not cleaned:
        raise ValueError(f"La variable de entorno {name} está vacía.")
    return cleaned


def get_target_regions(bootstrap_identity: oci.identity.IdentityClient) -> list[str]:
    specific_region = _clean_env_value(os.environ.get("OCI_TARGET_REGION", ""))
    if specific_region:
        return [specific_region]

    catalog_path = os.environ.get("OCI_REGIONS_JSON_PATH", "oci_public_regions.json")
    if os.path.exists(catalog_path):
        regions = load_regions_from_catalog(catalog_path)
        logging.info(
            "Regiones cargadas desde catálogo (%s): %s",
            catalog_path,
            ", ".join(regions),
        )
        return regions

    regions = get_realm_regions(bootstrap_identity)
    logging.info(
        "Catálogo no encontrado (%s). Se usa list_regions del realm actual: %s",
        catalog_path,
        ", ".join(regions),
    )
    return regions


def check_capacity_all_regions() -> tuple[ScanContext, list[CapacityResult], list[CapacityResult], dict, str]:
    private_key_pem = _get_required_env("OCI_PRIVATE_KEY_PEM")
    private_key_pem = private_key_pem.replace("\\n", "\n")

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as key_file:
        key_file.write(private_key_pem)
        key_path = key_file.name

    config = build_oci_config(key_path)
    context = now_context(config["region"])

    tenancy_ocid = _get_required_env("OCI_TENANCY_OCID")
    bootstrap_identity = oci.identity.IdentityClient(config)
    regions = get_target_regions(bootstrap_identity)

    all_results: list[CapacityResult] = []
    for region in regions:
        all_results.extend(scan_region(config, tenancy_ocid, region, context.timestamp_utc))

    hits = [res for res in all_results if has_capacity_hit(res)]
    return context, all_results, hits, config, key_path


def build_email_body(context: ScanContext, hits: list[CapacityResult]) -> str:
    timestamp_utc = context.timestamp_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    timestamp_madrid = context.timestamp_madrid.strftime("%Y-%m-%d %H:%M:%S %Z")
    repo = repo_name_from_env()

    lines = [
        "Se detectó capacidad OCI para VM.Standard.A1.Flex (4 OCPU / 24 GB).",
        "",
        f"Repositorio: {repo}",
        f"Región bootstrap (OCI_REGION): {context.bootstrap_region}",
        f"Timestamp UTC: {timestamp_utc}",
        f"Timestamp Europe/Madrid: {timestamp_madrid}",
        f"Número de hits: {len(hits)}",
        "",
        format_hits_table(hits),
    ]
    return "\n".join(lines)


def build_stack_success_email(context: ScanContext, stack_id: str, job_id: str) -> tuple[str, str]:
    timestamp_utc = context.timestamp_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    timestamp_madrid = context.timestamp_madrid.strftime("%Y-%m-%d %H:%M:%S %Z")

    subject = "OCI Madrid 3: stack lanzado automáticamente"
    body = "\n".join(
        [
            "Se detectó hueco en eu-madrid-3 y se lanzó el stack automáticamente.",
            f"Stack OCID: {stack_id}",
            f"Job OCID: {job_id}",
            f"Timestamp UTC: {timestamp_utc}",
            f"Timestamp Europe/Madrid: {timestamp_madrid}",
        ]
    )
    return subject, body


def _is_apply_job(job) -> bool:
    operation = getattr(job, "operation", None)
    if not operation:
        return False
    return str(operation).upper() == "APPLY"


def _extract_deployed_at(job) -> datetime | None:
    for attr in ("time_finished", "time_updated", "time_created"):
        value = getattr(job, attr, None)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    return None


def get_latest_successful_apply_job(
    rm_client, stack_id: str, compartment_id: str | None = None
):
    try:
        list_jobs_kwargs = {"stack_id": stack_id}
        if compartment_id:
            list_jobs_kwargs["compartment_id"] = compartment_id
        response = rm_client.list_jobs(**list_jobs_kwargs)
    except ServiceError as exc:
        logging.warning(
            "No se pudieron listar jobs del stack (code=%s, status=%s).",
            getattr(exc, "code", "UNKNOWN"),
            getattr(exc, "status", "UNKNOWN"),
        )
        return None
    except TypeError as exc:
        logging.warning(
            "No se pudieron listar jobs del stack por parámetros incompletos: %s",
            exc,
        )
        return None

    succeeded_apply_jobs = [
        job
        for job in response.data
        if _is_apply_job(job) and str(getattr(job, "lifecycle_state", "")).upper() == "SUCCEEDED"
    ]
    if not succeeded_apply_jobs:
        return None

    succeeded_apply_jobs.sort(
        key=lambda job: _extract_deployed_at(job) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return succeeded_apply_jobs[0]


def should_send_daily_stack_email(context: ScanContext, deployed_at: datetime) -> StackNotificationPlan:
    deployed_madrid_date = deployed_at.astimezone(MADRID_TZ).date()
    today_madrid = context.timestamp_madrid.date()
    days_since = (today_madrid - deployed_madrid_date).days

    if days_since < 0 or days_since > 2:
        return StackNotificationPlan(False, None, deployed_at)

    if context.timestamp_madrid.hour != 9 or context.timestamp_madrid.minute != 0:
        return StackNotificationPlan(False, days_since + 1, deployed_at)

    return StackNotificationPlan(True, days_since + 1, deployed_at)


def build_stack_daily_email(plan: StackNotificationPlan, stack_id: str) -> tuple[str, str]:
    deployed_at = plan.deployed_at.astimezone(MADRID_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    subject = f"Recordatorio stack OCI (día {plan.day_number}/3)"
    body = "\n".join(
        [
            "Tu stack ya está subido automáticamente en OCI Madrid 3.",
            f"Stack OCID: {stack_id}",
            f"Fecha de despliegue (Europe/Madrid): {deployed_at}",
            f"Este es el aviso del día {plan.day_number} de 3.",
        ]
    )
    return subject, body


def maybe_launch_stack(context: ScanContext, config: dict, hits: list[CapacityResult]) -> None:
    stack_id = _normalize_ocid(os.environ.get("OCI_STACK_ID"))
    compartment_id = _normalize_ocid(os.environ.get("OCI_STACK_COMPARTMENT_OCID"))

    if not stack_id:
        logging.info(
            "OCI_STACK_ID no definido. Solo se notificará capacidad."
        )
        return

    rm_client = oci.resource_manager.ResourceManagerClient(config)
    latest_success = get_latest_successful_apply_job(rm_client, stack_id, compartment_id)

    if latest_success is not None:
        deployed_at = _extract_deployed_at(latest_success)
        if not deployed_at:
            logging.info("El stack ya tiene APPLY exitoso, pero sin timestamp disponible.")
            return

        plan = should_send_daily_stack_email(context, deployed_at)
        if plan.should_send:
            subject, body = build_stack_daily_email(plan, stack_id)
            send_email(subject, body)
            logging.info("Email diario de stack enviado (día %s/3).", plan.day_number)
        else:
            logging.info("No toca email diario en esta ejecución.")
        return

    if not hits:
        logging.info("Sin hueco detectado en Madrid3. No se lanza el stack.")
        return

    details = oci.resource_manager.models.CreateJobDetails(
        stack_id=stack_id,
        display_name=f"auto-apply-madrid3-{context.timestamp_utc.strftime('%Y%m%d%H%M%S')}",
        operation_details=oci.resource_manager.models.CreateApplyJobOperationDetails(
            execution_plan_strategy="AUTO_APPROVED"
        ),
    )
    response = rm_client.create_job(details)
    job_id = response.data.id

    subject, body = build_stack_success_email(context, stack_id, job_id)
    send_email(subject, body)
    logging.info("Stack lanzado automáticamente. Job=%s", job_id)


def main() -> None:
    configure_logging()
    context, all_results, hits, config, key_path = check_capacity_all_regions()

    try:
        logging.info("Escaneo completado. Resultados=%s | Hits=%s", len(all_results), len(hits))

        if hits:
            subject = f"OCI capacidad disponible: {len(hits)} hit(s)"
            body = build_email_body(context, hits)
            send_email(subject, body)
            logging.info("Se envió un único email resumen para esta ejecución.")
        else:
            logging.info("No se encontraron hits de capacidad disponible.")

        maybe_launch_stack(context, config, hits)
    finally:
        try:
            os.remove(key_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
