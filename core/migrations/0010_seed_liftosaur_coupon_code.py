"""Seeds the VERYLIFT coupon code onto the Liftosaur row created by
0008_seed_supported_apps, so the supported-apps page's featured card can
show the code (via components/_copy_code.html, the same partial the
existing onboarding/settings coupon CTA uses) alongside its own CTA and
disclosure.
"""

from django.db import migrations

COUPON_CODE = "VERYLIFT"


def set_liftosaur_coupon_code(apps, schema_editor):
    SupportedApp = apps.get_model("core", "SupportedApp")
    SupportedApp.objects.filter(name="Liftosaur").update(coupon_code=COUPON_CODE)


def clear_liftosaur_coupon_code(apps, schema_editor):
    SupportedApp = apps.get_model("core", "SupportedApp")
    SupportedApp.objects.filter(name="Liftosaur").update(coupon_code="")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_supportedapp_coupon_code"),
    ]

    operations = [
        migrations.RunPython(set_liftosaur_coupon_code, clear_liftosaur_coupon_code),
    ]
