from django import forms


class NewsletterSubscribeForm(forms.Form):
    email = forms.EmailField()
