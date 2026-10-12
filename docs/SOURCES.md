# Sources for Google Play facts used in the recommendation

Checked 11 Oct 2026.

| Fact | Source |
|---|---|
| The published rating "is weighted towards more recent ratings"; new ratings do not immediately affect it | [Play Console Help – View and analyze your app's ratings and reviews](https://support.google.com/googleplay/android-developer/answer/138230) |
| After a developer reply, the user "receive[s] a push notification and an email notification" and can update their rating or review at any time | same page |
| Replies must directly address the user's comment, be respectful, and not solicit | same page |
| The in-app review card must not be preceded by questions such as "Do you like the app?" or "Would you rate this app 5 stars"; it should not be triggered from a button; Google applies a time-bound quota | [Android Developers – Google Play In-App Reviews API](https://developer.android.com/guide/playcore/in-app-review) |
| Developer replies longer than about 350 characters are rejected | [Google Play Developer API – reviews.reply](https://developers.google.com/android-publisher/api-ref/rest/v3/reviews/reply) |
| Android vitals bad-behaviour thresholds: user-perceived crash rate 1.09% overall / 8% per phone model; user-perceived ANR rate 0.47% / 8%. Above them, Play "may reduce the visibility" of the app and "may also show users a warning on your store listing"; assessed on a 28-day average | [Android Developers – Android vitals](https://developer.android.com/topic/performance/vitals) |
| Play Integrity remediation dialogs: `GET_LICENSED` lets a user flagged as not installed from Play get a Play licence; `GET_INTEGRITY` also resolves remediable exceptions such as outdated Play services or network errors (check `isRemediable()`) | [Android Developers – Play Integrity remediation](https://developer.android.com/google/play/integrity/remediation) |
| Since 2019 the Play rating favours recent ratings over the lifetime average; published counts are rescaled to match the weighted score | [Search Engine Land (2019)](https://searchengineland.com/new-app-rating-formula-headed-to-google-play-316697), [Gummicube](https://www.gummicube.com/blog/google-play-changes-how-ratings-are-weighted/) |
