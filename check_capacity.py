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
    diagnostic: str


@dataclass
class ScanContext:
    bootstrap_region: str
    timestamp_utc: datetime
    timestamp_madrid: datetime


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
        "user": os.environ["OCI_USER_OCID"],
        "tenancy": os.environ["OCI_TENANCY_OCID"],
        "fingerprint": os.environ["OCI_FINGERPRINT"],
        "key_file": private_key_path,
        "region": os.environ.get("OCI_REGION", "eu-madrid-3"),
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


def list_region_ads(identity_client: oci.identity.IdentityClient, tenancy_ocid: str) -> list[str]:
    ads = identity_client.list_availability_domains(compartment_id=tenancy_ocid).data
    return [ad.name for ad in ads if getattr(ad, "name", None)]


def _status_line(result: CapacityResult) -> str:
    return (
        f"region={result.region} | ad={result.availability_domain} | status={result.status} "
        f"| available_count={result.available_count} | diagnostic={result.diagnostic}"
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
    header = "region | availability_domain | status | available_count"
    sep = "-" * len(header)
    rows = [header, sep]
    for res in results:
        rows.append(
            f"{res.region} | {res.availability_domain} | {res.status} | {res.available_count}"
        )
    return "\n".join(rows)


def repo_name_from_env() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "(desconocido)")


def check_capacity_all_regions() -> tuple[ScanContext, list[CapacityResult], list[CapacityResult]]:
    private_key_pem = os.environ["OCI_PRIVATE_KEY_PEM"]

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as key_file:
        key_file.write(private_key_pem)
        key_path = key_file.name

    try:
        config = build_oci_config(key_path)
        context = now_context(config["region"])

        tenancy_ocid = os.environ["OCI_TENANCY_OCID"]
        bootstrap_identity = oci.identity.IdentityClient(config)

        regions = get_realm_regions(bootstrap_identity)
        logging.info(
            "Regiones detectadas en el realm actual: %s",
            ", ".join(regions),
        )

        all_results: list[CapacityResult] = []
        for region in regions:
            all_results.extend(scan_region(config, tenancy_ocid, region))

        hits = [res for res in all_results if has_capacity_hit(res)]
        return context, all_results, hits
    finally:
        try:
            os.remove(key_path)
        except OSError:
            pass


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


def main() -> None:
    configure_logging()
    context, all_results, hits = check_capacity_all_regions()

    logging.info("Escaneo completado. Resultados=%s | Hits=%s", len(all_results), len(hits))

    if hits:
        subject = f"OCI capacidad disponible: {len(hits)} hit(s)"
        body = build_email_body(context, hits)
        send_email(subject, body)
        logging.info("Se envió un único email resumen para esta ejecución.")
    else:
        logging.info("No se encontraron hits de capacidad disponible.")


if __name__ == "__main__":
    main()
