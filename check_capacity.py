import os
import smtplib
import ssl
import tempfile
from email.message import EmailMessage

import oci

TARGET_SHAPE = "VM.Standard.A1.Flex"
TARGET_OCPUS = 4
TARGET_MEMORY_GB = 24


def send_email(subject: str, body: str) -> None:
    email_user = os.environ["EMAIL_USER"]
    email_password = os.environ["EMAIL_APP_PASSWORD"]
    email_to = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_user
    msg["To"] = email_to
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
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


def get_first_availability_domain(identity_client, tenancy_ocid: str) -> str:
    ads = identity_client.list_availability_domains(compartment_id=tenancy_ocid).data
    if not ads:
        raise RuntimeError("No se encontró ningún Availability Domain.")
    return ads[0].name


def check_capacity():
    private_key_pem = os.environ["OCI_PRIVATE_KEY_PEM"]

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as key_file:
        key_file.write(private_key_pem)
        key_path = key_file.name

    try:
        config = build_oci_config(key_path)
        identity_client = oci.identity.IdentityClient(config)
        compute_client = oci.core.ComputeClient(config)

        tenancy_ocid = os.environ["OCI_TENANCY_OCID"]
        ad_name = get_first_availability_domain(identity_client, tenancy_ocid)

        details = oci.core.models.CreateComputeCapacityReportDetails(
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

        response = compute_client.create_compute_capacity_report(details)
        report = response.data

        found_available = False
        lines = []

        for item in report.shape_availabilities:
            status = getattr(item, "availability_status", "UNKNOWN")
            available_count = getattr(item, "available_count", 0)
            lines.append(
                f"AD={ad_name} | shape={item.instance_shape} | "
                f"status={status} | available_count={available_count}"
            )
            if status == "AVAILABLE" and int(available_count or 0) > 0:
                found_available = True

        return found_available, "\n".join(lines)

    finally:
        try:
            os.remove(key_path)
        except OSError:
            pass


def main():
    available, details = check_capacity()

    if available:
        send_email(
            "OCI Madrid 3: hay capacidad disponible",
            f"Se ha detectado capacidad para VM.Standard.A1.Flex (4 OCPU / 24 GB) en eu-madrid-3.\n\n{details}",
        )
        print("Capacidad disponible. Email enviado.")
    else:
        print("Aún sin capacidad.")
        print(details)


if __name__ == "__main__":
    main()
