from django import forms


class PolicyConsentForm(forms.Form):
    agreed = forms.BooleanField(
        required=True,
        label="I have read and agree to the above policies",
    )
