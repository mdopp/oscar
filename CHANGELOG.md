# Changelog

## [0.39.5](https://github.com/mdopp/solarisbay/compare/v0.39.4...v0.39.5) (2026-08-22)


### Bug Fixes

* **template:** bind the batch transcription endpoint to loopback ([73aef96](https://github.com/mdopp/solarisbay/commit/73aef969195e455b66b11be065a7f1b99551ab7a))
* **template:** bind the batch transcription endpoint to loopback ([aa72ea5](https://github.com/mdopp/solarisbay/commit/aa72ea5469192ca983a66b8f14b996c19e046ef8))
* **template:** remember the batch-whisper switch across deploys ([0578ac8](https://github.com/mdopp/solarisbay/commit/0578ac833072a6bdec6e76e6ec1fab89fd7e2ec2))
* **template:** remember the batch-whisper switch across deploys ([002f635](https://github.com/mdopp/solarisbay/commit/002f63535c01a74ea558c88469d60b08059e44c5)), closes [#1200](https://github.com/mdopp/solarisbay/issues/1200)

## [0.39.4](https://github.com/mdopp/solarisbay/compare/v0.39.3...v0.39.4) (2026-08-18)


### Bug Fixes

* **template:** bind the GPU voice companions to loopback ([cf07e6f](https://github.com/mdopp/solarisbay/commit/cf07e6fd7cce4057933569621b0b15cce655280a))
* **template:** bind the GPU voice companions to loopback ([aefebed](https://github.com/mdopp/solarisbay/commit/aefebed00421bb0e0da7acbb6bf8932db54f1da4)), closes [#1196](https://github.com/mdopp/solarisbay/issues/1196)

## [0.39.3](https://github.com/mdopp/solarisbay/compare/v0.39.2...v0.39.3) (2026-08-18)


### Bug Fixes

* **chat:** expire a held confirmation instead of firing it on a later yes ([1952ca4](https://github.com/mdopp/solarisbay/commit/1952ca4e5208d66bcc2b5b38bcbf40d2d00f3ac5)), closes [#1183](https://github.com/mdopp/solarisbay/issues/1183)
* **chat:** expire held confirmations and scope the enrolment wizard per dialog ([4ac89d2](https://github.com/mdopp/solarisbay/commit/4ac89d2f5b6c7af8a33a40840349ebfc0d5bd242))
* **chat:** scope chat sessions traces and admin actions to the caller ([5ea82ec](https://github.com/mdopp/solarisbay/commit/5ea82ecb93f5f09841b72a40b55fedaf238a7966))
* **chat:** scope chat sessions traces and admin actions to the caller ([7061349](https://github.com/mdopp/solarisbay/commit/706134914df807e57e3d893d784f21091bcce371)), closes [#1168](https://github.com/mdopp/solarisbay/issues/1168) [#1169](https://github.com/mdopp/solarisbay/issues/1169) [#1170](https://github.com/mdopp/solarisbay/issues/1170) [#1171](https://github.com/mdopp/solarisbay/issues/1171)
* **chat:** scope the enrolment wizard to the dialog that started it ([bac3ed1](https://github.com/mdopp/solarisbay/commit/bac3ed1916da480d55101a25389299da33cea5db)), closes [#1184](https://github.com/mdopp/solarisbay/issues/1184)
* **gatekeeper:** expire abandoned enrolment embeddings ([6d9480c](https://github.com/mdopp/solarisbay/commit/6d9480c16a69e9fa8fad4791931a432ebed16b9c))
* **gatekeeper:** expire abandoned enrolment embeddings ([5e953bf](https://github.com/mdopp/solarisbay/commit/5e953bff4092ec8dc6ac0bc0b55063dc5047ee7c)), closes [#1175](https://github.com/mdopp/solarisbay/issues/1175)
* **skill:** make the enrolment prose match the argument-less tool ([284ae82](https://github.com/mdopp/solarisbay/commit/284ae828342ce61197ed42ca7a49c52ff12225a8)), closes [#1185](https://github.com/mdopp/solarisbay/issues/1185)

## [0.39.2](https://github.com/mdopp/solarisbay/compare/v0.39.1...v0.39.2) (2026-08-18)


### Bug Fixes

* **autoloop:** count a batch by its issues not its units ([aeca24d](https://github.com/mdopp/solarisbay/commit/aeca24d8c50089082bcb04562b5dfc9d1f9e1196)), closes [#1192](https://github.com/mdopp/solarisbay/issues/1192)
* **autoloop:** keep a security unit off the shared batch ([d687cba](https://github.com/mdopp/solarisbay/commit/d687cba2b521acda186033965d854796e87dd532)), closes [#1178](https://github.com/mdopp/solarisbay/issues/1178)
* batch 2026-08-17b - loopback voice ports, per-room timers, skill drafting, contacts import isolation ([988e23a](https://github.com/mdopp/solarisbay/commit/988e23a7f1360c3ac02236d603e56f531acda2a1))
* cap the gatekeeper audio buffer, pin schema-init, and tear down stale units ([7cbc9c6](https://github.com/mdopp/solarisbay/commit/7cbc9c646293346ee7c8b899a1706b603b18c9b6))
* **chat:** give the household turn a tool that reaches the pending skills dir ([5a38b6e](https://github.com/mdopp/solarisbay/commit/5a38b6e0c7d4ce701f0d41da526a1941f861a8cd)), closes [#1188](https://github.com/mdopp/solarisbay/issues/1188)
* **chat:** isolate per-card contacts import failures and key UID-less cards by person ([e055082](https://github.com/mdopp/solarisbay/commit/e05508268af2e18bcc07916d5abc9d094f5425db)), closes [#1189](https://github.com/mdopp/solarisbay/issues/1189) [#1190](https://github.com/mdopp/solarisbay/issues/1190)
* **chat:** reduce notes tool paths to one vault-relative form ([d35428b](https://github.com/mdopp/solarisbay/commit/d35428bfedf5d4e0f88cbe9e1ef7fe23f02c1a3a)), closes [#1173](https://github.com/mdopp/solarisbay/issues/1173)
* **chat:** ring a fired timer only where it was set ([164b109](https://github.com/mdopp/solarisbay/commit/164b1098aa630d1521dd827dd92446e3ba8f2c69)), closes [#1187](https://github.com/mdopp/solarisbay/issues/1187)
* **db:** pin alembic and sqlalchemy in the schema-init image ([6d7af1c](https://github.com/mdopp/solarisbay/commit/6d7af1caef03df8cfac11fa3a971955dac32a203)), closes [#1172](https://github.com/mdopp/solarisbay/issues/1172)
* **gatekeeper:** cap the per-connection audio buffer ([b4d61bc](https://github.com/mdopp/solarisbay/commit/b4d61bc5322a681e53ee59ee6f780d424286c23d)), closes [#1174](https://github.com/mdopp/solarisbay/issues/1174)
* **template:** bind the CPU voice Wyoming ports to loopback ([7d80d9a](https://github.com/mdopp/solarisbay/commit/7d80d9ad16c223334474f089f2ce27aaef4a6dea)), closes [#1186](https://github.com/mdopp/solarisbay/issues/1186)
* **template:** degrade the whisper hint wrapper instead of refusing to start ([b28737d](https://github.com/mdopp/solarisbay/commit/b28737d778ca8ab7fe37a4b64f19ff6d8928157f))
* **template:** degrade the whisper hint wrapper instead of refusing to start ([88ef5fd](https://github.com/mdopp/solarisbay/commit/88ef5fd2a0716f77f1c0c4255084edf96cac493f)), closes [#1193](https://github.com/mdopp/solarisbay/issues/1193)
* **template:** remove the wakeword-trainer unit when CDI is gone ([6fefed5](https://github.com/mdopp/solarisbay/commit/6fefed5edfad7c22a66ffa0e979a5479ddbfad9a)), closes [#1180](https://github.com/mdopp/solarisbay/issues/1180)
* **template:** remove the wakeword-trainer unit when its flag is off ([a0de939](https://github.com/mdopp/solarisbay/commit/a0de939f89f584cfababce1ebb0b8569d1f53a3d)), closes [#1176](https://github.com/mdopp/solarisbay/issues/1176)

## [0.39.1](https://github.com/mdopp/solarisbay/compare/v0.39.0...v0.39.1) (2026-08-15)


### Bug Fixes

* **template:** filter non-speech before whisper decodes device names ([ec7316f](https://github.com/mdopp/solarisbay/commit/ec7316fb5dd9a08328f74a02e9d6007cdbe306b7))
* **template:** filter non-speech before whisper decodes device names ([9dc14fe](https://github.com/mdopp/solarisbay/commit/9dc14fef52ce6f7fa98f69be93f8a4288134faa7)), closes [#1158](https://github.com/mdopp/solarisbay/issues/1158)
* **template:** restart a voice unit when its mounted assets change ([441e3a4](https://github.com/mdopp/solarisbay/commit/441e3a4fb9bc4f46d4cf64d2ae3794f565e11996))
* **template:** restart a voice unit when its mounted assets change ([f9a5675](https://github.com/mdopp/solarisbay/commit/f9a5675becf85129f982a209afa03664ed2eb3d3)), closes [#1166](https://github.com/mdopp/solarisbay/issues/1166)

## [0.39.0](https://github.com/mdopp/solarisbay/compare/v0.38.0...v0.39.0) (2026-08-14)


### Features

* **template:** let a whisper request carry its own words ([deba59a](https://github.com/mdopp/solarisbay/commit/deba59a6492e3ae9f5dcb000b4506fd6490451dd)), closes [#1157](https://github.com/mdopp/solarisbay/issues/1157)
* **template:** let a whisper request carry its own words ([3fc98a0](https://github.com/mdopp/solarisbay/commit/3fc98a0520cf85acbd2cd513289e86d65f8703d4)), closes [#1157](https://github.com/mdopp/solarisbay/issues/1157)
* **template:** per-request word hints and a timestamped segment endpoint for solaris-whisper ([3173dcb](https://github.com/mdopp/solarisbay/commit/3173dcb0180d8d7af01558bf8ce99f1d836ed1cc))
* **template:** put whisper on the GPU, per-request hints, and a batch transcription unit ([114fb58](https://github.com/mdopp/solarisbay/commit/114fb58b0dbf970f39b85e03d3022c39d20d70b3))
* **template:** serve timestamped whisper segments from a path ([b6445fd](https://github.com/mdopp/solarisbay/commit/b6445fdbe451b1ca8a4d9c22c743e69818fe5018)), closes [#1157](https://github.com/mdopp/solarisbay/issues/1157)
* **template:** transcribe recordings in a second GPU whisper container ([28c1ad6](https://github.com/mdopp/solarisbay/commit/28c1ad67d582f66cf9a8fa6c5e40024f96d81f38)), closes [#1161](https://github.com/mdopp/solarisbay/issues/1161)


### Bug Fixes

* **gatekeeper:** carry the speaker-ID verdict as an explicit matched flag ([6eebb84](https://github.com/mdopp/solarisbay/commit/6eebb8435a72bd4cb51a1db6a5aad74595aa42e8))
* **gatekeeper:** carry the speaker-ID verdict as an explicit matched flag ([4d70830](https://github.com/mdopp/solarisbay/commit/4d708304c3b91cba0a0b6f49cb32e86fb20f6dd0)), closes [#1152](https://github.com/mdopp/solarisbay/issues/1152)
* **template:** revert per-request whisper hints, real-load regression ([#1160](https://github.com/mdopp/solarisbay/issues/1160)) ([f4a4996](https://github.com/mdopp/solarisbay/commit/f4a4996bfb9599a506939d7f3ea7acf1d8e15b37)), closes [#1157](https://github.com/mdopp/solarisbay/issues/1157)
* **template:** run solaris-whisper on the GPU, not the CPU ([e1cb96f](https://github.com/mdopp/solarisbay/commit/e1cb96f67866ec3d4541d18144cb2b1bc04573eb)), closes [#1162](https://github.com/mdopp/solarisbay/issues/1162)

## [0.38.0](https://github.com/mdopp/solarisbay/compare/v0.37.0...v0.38.0) (2026-08-13)


### Features

* **chat:** add calendar_create writing straight to Radicale via dav_client ([3a356dc](https://github.com/mdopp/solarisbay/commit/3a356dc6d8b414ab124e58c97cb024579c103859)), closes [#1125](https://github.com/mdopp/solarisbay/issues/1125) [#1126](https://github.com/mdopp/solarisbay/issues/1126)
* **chat:** attribute the prefill to tools, soul, registry and scaffold ([544c0dd](https://github.com/mdopp/solarisbay/commit/544c0dd6f2526f5cd467300c8d547677c692ea5a)), closes [#1138](https://github.com/mdopp/solarisbay/issues/1138)
* **chat:** declare a visibility class on every tool and enforce it on voice ([4eeaa1d](https://github.com/mdopp/solarisbay/commit/4eeaa1d81eb44c107f6fae6d22e507c091bee6d4))
* **chat:** declare a visibility class on every tool and enforce it on voice ([72679df](https://github.com/mdopp/solarisbay/commit/72679df6e2ef0aa5bfcdf3d4c097e5c5f2dc0cc5)), closes [#1130](https://github.com/mdopp/solarisbay/issues/1130)
* **chat:** emit dated tasks as VTODO instead of a calendar event ([1ea4697](https://github.com/mdopp/solarisbay/commit/1ea4697f6acf8966844e711468bc93bbeb2edae0)), closes [#1127](https://github.com/mdopp/solarisbay/issues/1127)
* **chat:** hold every number and date in an answer against the retrieved record ([a0f286a](https://github.com/mdopp/solarisbay/commit/a0f286a69abcda929a3a334b79701e663c38b703)), closes [#1129](https://github.com/mdopp/solarisbay/issues/1129)
* **chat:** remove the web-search path and the dead prompt text it fed ([7c26dbd](https://github.com/mdopp/solarisbay/commit/7c26dbd81c80772afec03ffcdcc254bc8cd3816b)), closes [#1122](https://github.com/mdopp/solarisbay/issues/1122) [#1123](https://github.com/mdopp/solarisbay/issues/1123)
* **chat:** retire solaris-deep and the web-search path, add calendar_create and a grounding checker ([930a8ea](https://github.com/mdopp/solarisbay/commit/930a8ea695f156bbb358ceff213d3beb1f211035))
* **chat:** retire the solaris-deep profile and run the night jobs on household ([35c1ad6](https://github.com/mdopp/solarisbay/commit/35c1ad6e850e36120a597e9a5dfedd09bc8cbbca)), closes [#1121](https://github.com/mdopp/solarisbay/issues/1121)
* **chat:** sync personal .contacts persons back to a per-resident CardDAV book ([#1118](https://github.com/mdopp/solarisbay/issues/1118)) ([a1b207b](https://github.com/mdopp/solarisbay/commit/a1b207b4e90a78c459b1713ed89383aead92668a))
* port both MCP ends to the 2.x SDK and lift the &lt;2 caps ([de1a527](https://github.com/mdopp/solarisbay/commit/de1a527abea10d1a27d9e570b68b20e5edb8bea9)), closes [#1106](https://github.com/mdopp/solarisbay/issues/1106) [#1102](https://github.com/mdopp/solarisbay/issues/1102)
* **template:** prime whisper with the household's own device names ([b66055a](https://github.com/mdopp/solarisbay/commit/b66055a5aef84b8b6db458bb0e0d75342425b5b5))
* **template:** prime whisper with the household's own device names ([b95c5bb](https://github.com/mdopp/solarisbay/commit/b95c5bb707a2c3b11a81ffe209e2afbd9a571763)), closes [#1142](https://github.com/mdopp/solarisbay/issues/1142)


### Bug Fixes

* **autoloop:** keep park and verify-set from corrupting the queue.py cache ([feeffa3](https://github.com/mdopp/solarisbay/commit/feeffa31ae76bdc6396b5ace3d4f734b5380db88)), closes [#1139](https://github.com/mdopp/solarisbay/issues/1139) [#1140](https://github.com/mdopp/solarisbay/issues/1140)
* **chat:** bench the prefill the household actually pays ([dd0450b](https://github.com/mdopp/solarisbay/commit/dd0450b6c1409340bc2d228dac6bbc347eb082af))
* **chat:** bench the prefill the household actually pays ([6491bc3](https://github.com/mdopp/solarisbay/commit/6491bc3d2e8ccd6d9d649601ce2f4b10a1351930)), closes [#1149](https://github.com/mdopp/solarisbay/issues/1149)
* **chat:** classify calendar_create as Persoenlich ([48499c9](https://github.com/mdopp/solarisbay/commit/48499c94b0734dce72e0534c389a5c1a755249f3))
* **chat:** gate Persoenlich only where speaker-ID actually runs ([6c36bec](https://github.com/mdopp/solarisbay/commit/6c36becfe783d7152eb0ed90960195e320fae1ea))
* **chat:** make calendar_create survive an already-existing collection ([#1137](https://github.com/mdopp/solarisbay/issues/1137)) ([0543361](https://github.com/mdopp/solarisbay/commit/05433611e4d0c5347798340ae120302a732cf47f))
* **ci:** disable the metadata-action latest flavor so only the raw rule moves latest ([#1116](https://github.com/mdopp/solarisbay/issues/1116)) ([14af723](https://github.com/mdopp/solarisbay/commit/14af72352bf6fe7ccc5c6ceaecebe66f6fe2e433)), closes [#1110](https://github.com/mdopp/solarisbay/issues/1110)
* **gatekeeper:** make the voice speaker-ID gate fail closed ([b4c30a0](https://github.com/mdopp/solarisbay/commit/b4c30a022c24ce8577100edcba9af63ef289fad0))
* **gatekeeper:** make the voice speaker-ID gate fail closed ([3f87e48](https://github.com/mdopp/solarisbay/commit/3f87e48df04086a1001dff64142fab15ad057a4a)), closes [#1146](https://github.com/mdopp/solarisbay/issues/1146)
* **template:** make the STT health probe self-contained ([19449e3](https://github.com/mdopp/solarisbay/commit/19449e3649070e3356008e2d371264db66003324))
* **template:** make the STT health probe self-contained ([f12b953](https://github.com/mdopp/solarisbay/commit/f12b953f38a7c5e2d19a991eb96681fd9daf2653)), closes [#1144](https://github.com/mdopp/solarisbay/issues/1144)
* **template:** re-stamp VAPID_PUBLIC_KEY when a reinstall blanks it ([fb3dabb](https://github.com/mdopp/solarisbay/commit/fb3dabb4621d1bd33ff877ab35a3c628343a986d))
* **template:** re-stamp VAPID_PUBLIC_KEY when a reinstall blanks it ([470ccb9](https://github.com/mdopp/solarisbay/commit/470ccb9be4785469f63463bf0203d07a340aec27)), closes [#1147](https://github.com/mdopp/solarisbay/issues/1147)

## [0.37.0](https://github.com/mdopp/solarisbay/compare/v0.36.1...v0.37.0) (2026-08-02)


### Features

* **gatekeeper:** store several voice fingerprints per resident and require a match margin ([#1107](https://github.com/mdopp/solarisbay/issues/1107)) ([a0196cb](https://github.com/mdopp/solarisbay/commit/a0196cbcf83a09e094e67fd686cd6a0fc1c09e3d)), closes [#1084](https://github.com/mdopp/solarisbay/issues/1084)


### Bug Fixes

* **chat:** converge person entities instead of minting one per contacts create ([#1112](https://github.com/mdopp/solarisbay/issues/1112)) ([ee12641](https://github.com/mdopp/solarisbay/commit/ee126412a19c899d7f39af621e449f74c053cbbf)), closes [#994](https://github.com/mdopp/solarisbay/issues/994)
* **ci:** let only a push to main move the latest image tag ([5d838ff](https://github.com/mdopp/solarisbay/commit/5d838ff1c7fd71cf1a81fb1d4cc484c451685c0f))
* **ci:** let only a push to main move the latest image tag ([5d838ff](https://github.com/mdopp/solarisbay/commit/5d838ff1c7fd71cf1a81fb1d4cc484c451685c0f)), closes [#1110](https://github.com/mdopp/solarisbay/issues/1110)

## [0.36.1](https://github.com/mdopp/solarisbay/compare/v0.36.0...v0.36.1) (2026-07-28)


### Bug Fixes

* **chat:** cap mcp below 2.0 and reconcile the mobile-app push doc ([#1108](https://github.com/mdopp/solarisbay/issues/1108)) ([67af6da](https://github.com/mdopp/solarisbay/commit/67af6da2410b539f6c967d5de4b98e16b8e9783c)), closes [#1095](https://github.com/mdopp/solarisbay/issues/1095) [#1102](https://github.com/mdopp/solarisbay/issues/1102) [#1106](https://github.com/mdopp/solarisbay/issues/1106)
* **template:** read the trainer image identity as JSON instead of a Go template ([#1104](https://github.com/mdopp/solarisbay/issues/1104)) ([37001bc](https://github.com/mdopp/solarisbay/commit/37001bc0fc7c9b7b9e2f7cd20ea4c20a092d4042)), closes [#1092](https://github.com/mdopp/solarisbay/issues/1092)

## [0.36.0](https://github.com/mdopp/solarisbay/compare/v0.35.0...v0.36.0) (2026-07-28)


### Features

* **chat:** Ask for the wake-word samples after the voice profile ([1803518](https://github.com/mdopp/solarisbay/commit/18035180243f09b3759fa5071b586866d12eba9b))
* **chat:** Authelia-gated page to decide one ServiceBay approval ([#1091](https://github.com/mdopp/solarisbay/issues/1091)) ([36c336b](https://github.com/mdopp/solarisbay/commit/36c336b1e7b9de1085727671ebcbb983b380067d)), closes [#1085](https://github.com/mdopp/solarisbay/issues/1085)
* **chat:** calendar cascade, note dual-file, .tool plugin system, contacts alias unify ([#1024](https://github.com/mdopp/solarisbay/issues/1024)) ([be0ee68](https://github.com/mdopp/solarisbay/commit/be0ee68860707e7d7a08ee0db66c9bf0e3434289))
* **chat:** Collect the wake-word samples in onboarding stage 2 ([dad2cbd](https://github.com/mdopp/solarisbay/commit/dad2cbd07206204b94974cba8059b5d4c807f4a3))
* **chat:** group Dokumente by shared Vorgang identifier ([3ac2a4f](https://github.com/mdopp/solarisbay/commit/3ac2a4ff99397b7f72068520c638e27e21ca434f)), closes [#1052](https://github.com/mdopp/solarisbay/issues/1052)
* **chat:** paperless AI-suggestions + poll-timeout fix + Vorgang grouping ([88553c5](https://github.com/mdopp/solarisbay/commit/88553c564249f21c0558d566e8eafdc4aaa34ac4))
* **chat:** paperless push adapter + tool plugins on /napi with renderer-agnostic cell-schema ([#1033](https://github.com/mdopp/solarisbay/issues/1033)) ([6215395](https://github.com/mdopp/solarisbay/commit/62153955526e4305353b556c559218643358c7ec))
* **chat:** read-only Dokumente portal + paperless push-timing decision ([6588b97](https://github.com/mdopp/solarisbay/commit/6588b971774cbdc5e354f1e12bafcd546a374467))
* **chat:** read-only Dokumente portal linking into the paperless UI ([ff70e15](https://github.com/mdopp/solarisbay/commit/ff70e151c76f10cb68df35fcc1d23beafaa20705)), closes [#1043](https://github.com/mdopp/solarisbay/issues/1043) [#1044](https://github.com/mdopp/solarisbay/issues/1044)
* **chat:** record the wake word "Solaris" in the browser and the app ([03a5a74](https://github.com/mdopp/solarisbay/commit/03a5a74642ae4e0fce114181e3f73b105836f0fa))
* **chat:** record the wake word in the browser instead of over the Voice PE ([652777a](https://github.com/mdopp/solarisbay/commit/652777a64d296a72c09432b6ff4b22f3ba5b3a28))
* **chat:** train the wakeword now, on an explicit tap, with the real run state ([#1088](https://github.com/mdopp/solarisbay/issues/1088)) ([8b960af](https://github.com/mdopp/solarisbay/commit/8b960af9542ea23e0558075fb276caa6bb55638a))
* **db:** Add wakeword_training_runs queue table for the GPU trainer handshake ([aa517d5](https://github.com/mdopp/solarisbay/commit/aa517d5e38a97881e48da6192220bc27b2927180)), closes [#1066](https://github.com/mdopp/solarisbay/issues/1066)
* **gatekeeper:** Record the wake-word samples as real wav files ([89505c2](https://github.com/mdopp/solarisbay/commit/89505c23951c487732b3b7890db12687491ef623))
* **gatekeeper:** Self-test a voice profile before enrolment claims success ([#1096](https://github.com/mdopp/solarisbay/issues/1096)) ([0899608](https://github.com/mdopp/solarisbay/commit/08996087a76863098ea085e3cc55ea1370682977))
* **ingest:** converge a confirmed paperless document type onto the OKF category ([b82acf2](https://github.com/mdopp/solarisbay/commit/b82acf27687e3d49421a9163940bb06a6ab9f431))
* **ingest:** link a confirmed paperless correspondent to the document's contact ([fca05e5](https://github.com/mdopp/solarisbay/commit/fca05e517415f4fae159da5e7905fbd5841793fb))
* **ingest:** read confirmed paperless correspondent and document type back into OKF ([137894f](https://github.com/mdopp/solarisbay/commit/137894f1af17e7a8811e4ba5326827e029cff8bd))
* **ingest:** read paperless correspondent and document type on the ingest cron ([6f5c353](https://github.com/mdopp/solarisbay/commit/6f5c353849058ce97f39d1aadfafb15810468318))
* **media:** Raum-spezifische Standardgeraete via room-devices Note ([7cb077f](https://github.com/mdopp/solarisbay/commit/7cb077f7b7a6d300e7d7df79f8ef0a5f0383d7ef))
* **media:** vereinheitlichtes Media-Playback + OKF-Slug Fallback ([e01766d](https://github.com/mdopp/solarisbay/commit/e01766de9ceb281aa64482036b37c42de0e363d4))
* **media:** vollstaendig generische Radio-Erkennung fuer beliebige Sender ([e539f79](https://github.com/mdopp/solarisbay/commit/e539f79f8d350e87f781a0f24a5d4cb47a47c4a9))
* **template:** Deploy the wakeword trainer as a GPU companion Quadlet ([3d88854](https://github.com/mdopp/solarisbay/commit/3d8885474fd3b71f05c7172428aa64e588e519cb)), closes [#1066](https://github.com/mdopp/solarisbay/issues/1066)
* **template:** enable paperless AI suggestions + trigger after push ([b45cfc1](https://github.com/mdopp/solarisbay/commit/b45cfc1d4723d66a56e63e38f8d0d58c4cfae36b)), closes [#1050](https://github.com/mdopp/solarisbay/issues/1050)
* **template:** managed paperless admin + auto-mint DRF token wired into engine ([#1035](https://github.com/mdopp/solarisbay/issues/1035)) ([e94ae4b](https://github.com/mdopp/solarisbay/commit/e94ae4b6c0432e17f9b460cdc8db9d57ea48e261)), closes [#1034](https://github.com/mdopp/solarisbay/issues/1034)
* **template:** one language setting for the voice stack ([8386696](https://github.com/mdopp/solarisbay/commit/8386696d5dd1a0a67001c64df8db431ea9ebfd12))
* **template:** one language setting for the voice stack ([1da43a6](https://github.com/mdopp/solarisbay/commit/1da43a6380b266047acc41a36bfd56d932e71f4e)), closes [#1057](https://github.com/mdopp/solarisbay/issues/1057)
* **template:** paperless-ngx document store + durable radicale-rights self-heal ([#1027](https://github.com/mdopp/solarisbay/issues/1027)) ([053be26](https://github.com/mdopp/solarisbay/commit/053be26bf156a2389b6c3635ac266e0266bd3df0))
* **voice:** Automatic System-UID Resident Resolver ('Michael' -&gt; 'mdopp') ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([4d52a13](https://github.com/mdopp/solarisbay/commit/4d52a135d47efd5d7ee7626f006de300477fd58f))
* **voice:** Bidirectional Onboarding Flow & Buchstabier-Parser ('M-A-R-C-O' -&gt; 'marco') ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([eb99b60](https://github.com/mdopp/solarisbay/commit/eb99b6044a9f8c43b1e62f0320a303e69943d033))
* **voice:** Buchstabierte Bestätigung der User-ID ('Michael Dopp wurde als M-D-O-P-P erkannt') ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([3b91f8e](https://github.com/mdopp/solarisbay/commit/3b91f8e18add474c21332d2565dee4e9a42a5101))
* **voice:** dedicated ephemeral solaris-enrollment profile ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([824d4e0](https://github.com/mdopp/solarisbay/commit/824d4e0ca643248a63f698f35986038466271663))
* **voice:** Implement zero-LLM deterministic FSM state machine for voice enrollment ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([bdc9f49](https://github.com/mdopp/solarisbay/commit/bdc9f49dc52ed6ec6894c72b26020cdd92697931))
* **voice:** Interactive Wakeword Improvement Skill & GPU Training Trigger ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([c9547b8](https://github.com/mdopp/solarisbay/commit/c9547b8599889fcd05cc9cf63307000089812b89))
* **voice:** Sample Metadata Storage, Automated Quality Audit & Selective Deletion ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([93a5777](https://github.com/mdopp/solarisbay/commit/93a57778f0ea4638cb9e03c391e8517ef0769b9c))
* **wakeword-trainer:** Add the GPU trainer image that claims queued training runs ([0bcf28a](https://github.com/mdopp/solarisbay/commit/0bcf28a21e11fc70b32d2762b1b75f7d9f28d070)), closes [#1066](https://github.com/mdopp/solarisbay/issues/1066)
* **wakeword:** hand the queue DB to the training script so it finds the clips ([9adf23a](https://github.com/mdopp/solarisbay/commit/9adf23a21ea21f3a305f8f3b406f32fb18e9590f)), closes [#1074](https://github.com/mdopp/solarisbay/issues/1074)
* **wakeword:** train on the residents' own recordings, not just Piper ([235ff0c](https://github.com/mdopp/solarisbay/commit/235ff0cd474fb391ea578cc5589651b990574a3a)), closes [#1074](https://github.com/mdopp/solarisbay/issues/1074)
* **wakeword:** train the model on the residents' own recordings ([41d523a](https://github.com/mdopp/solarisbay/commit/41d523ad22b426c29f9c74b31045a40e5a04028e))
* **wakeword:** Train the wake word in a dedicated GPU image via a DB queue ([89fab44](https://github.com/mdopp/solarisbay/commit/89fab44f6bc053b62d9aee6645cd1008ccb72526))


### Bug Fixes

* **chat:** drop hardcoded household devices from the play path ([4c59fda](https://github.com/mdopp/solarisbay/commit/4c59fda961f345051518e448d8e189dc0069cffb))
* **chat:** Enqueue wakeword training instead of a Popen that can never run ([4a8e3f6](https://github.com/mdopp/solarisbay/commit/4a8e3f6ba54e8691f7b0ef05762c87ca0d15a0ed)), closes [#1066](https://github.com/mdopp/solarisbay/issues/1066)
* **chat:** normalise paperless task-status case so pushes stop timing out ([397f8a6](https://github.com/mdopp/solarisbay/commit/397f8a63525a487d40108d029892544bff6ff5bb))
* **chat:** normalise paperless task-status case so pushes stop timing out ([f659465](https://github.com/mdopp/solarisbay/commit/f65946528832258c8fb3e2fe41eef6f86d3642f5)), closes [#1055](https://github.com/mdopp/solarisbay/issues/1055)
* **chat:** only make a trace row look clickable when it can open something ([b96ad89](https://github.com/mdopp/solarisbay/commit/b96ad89eeed875de5edfd8cd4d5634324ce51409))
* **chat:** only make a trace row look clickable when it can open something ([27ad197](https://github.com/mdopp/solarisbay/commit/27ad1977581329080fd52663086db011a868922e)), closes [#939](https://github.com/mdopp/solarisbay/issues/939)
* **chat:** read DRF-paginated tasks response in paperless push adapter ([fa3a642](https://github.com/mdopp/solarisbay/commit/fa3a642df18f7a06c010d305e03004bdddbb73a1))
* **chat:** read DRF-paginated tasks response in paperless push adapter ([3f1a7a9](https://github.com/mdopp/solarisbay/commit/3f1a7a936720dee6b40e5faffa45ea46e61c9e24)), closes [#1048](https://github.com/mdopp/solarisbay/issues/1048)
* **chat:** resolve enrollment status constant and future-import syntax ([ec821b9](https://github.com/mdopp/solarisbay/commit/ec821b9ab138d84aef60ee0f623e6272010f9689))
* **chat:** restore the honest-failure contract in the register tools ([d98878a](https://github.com/mdopp/solarisbay/commit/d98878a9e34b7ef447ef702640c763077055b368))
* **chat:** scope enrollment steering to the enrollment soul ([e66b7b6](https://github.com/mdopp/solarisbay/commit/e66b7b60b5cde2688d8eb489e57b4445735954ca))
* **chat:** show what the wakeword recorder is doing, and stop claiming it trains ([95a535f](https://github.com/mdopp/solarisbay/commit/95a535fe11fa776fc69b33197435ec188d8c89b9))
* **chat:** show what the wakeword recorder is doing, and stop claiming it trains ([0be5b98](https://github.com/mdopp/solarisbay/commit/0be5b980445dd9020854eb571c452b9924fecddb))
* **chat:** store wakeword samples next to the database ([2576513](https://github.com/mdopp/solarisbay/commit/25765137a5909d03c841e1df2886301faacffb53))
* **chat:** surface the real paperless push error instead of "0"/empty ([8ed8b66](https://github.com/mdopp/solarisbay/commit/8ed8b66e60835684aa241367cfac1861606a3a76))
* **chat:** surface the real paperless push error instead of "0"/empty ([5c78748](https://github.com/mdopp/solarisbay/commit/5c787488816a0892c165e598304d40264527688e)), closes [#1046](https://github.com/mdopp/solarisbay/issues/1046)
* **ci:** Actually skip the layer cache for the trainer image ([ab59014](https://github.com/mdopp/solarisbay/commit/ab59014098dc899218718fd1e085afebc8fbf261)), closes [#1066](https://github.com/mdopp/solarisbay/issues/1066)
* **engine:** Fix outer loop break in say short-circuit — prevent 2nd LLM pass execution ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([16c472f](https://github.com/mdopp/solarisbay/commit/16c472fd7a4fe8aec87daa9acbe1437599793941))
* **engine:** match paperless poll budget to real OCR + retry on timeout ([69c0192](https://github.com/mdopp/solarisbay/commit/69c0192a94dbef706ace0d0b08cfa2683b223586)), closes [#1053](https://github.com/mdopp/solarisbay/issues/1053)
* **engine:** scope the prompt to one conversation so old turns stop poisoning it ([4815a75](https://github.com/mdopp/solarisbay/commit/4815a75b60f9e91518615270b612f5fd829dec6d))
* **engine:** scope the prompt to one conversation so old turns stop poisoning it ([df0be8f](https://github.com/mdopp/solarisbay/commit/df0be8fc0b8eba4fa7684bea82fa5ad0600ae48a)), closes [#1067](https://github.com/mdopp/solarisbay/issues/1067)
* **facade:** Fix client.profile_name attribute name in facade turns generator ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([0ae8fe8](https://github.com/mdopp/solarisbay/commit/0ae8fe8216659f3e74e73e745903d266cea8d647))
* **facade:** Move from __future__ import annotations to line 1 to fix startup SyntaxError ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([92a5661](https://github.com/mdopp/solarisbay/commit/92a56612cbbc69e9dcdd930a89896a50228c8b42))
* **gatekeeper:** cap the mcp dependency below 2.0 ([#1101](https://github.com/mdopp/solarisbay/issues/1101)) ([9cfbf99](https://github.com/mdopp/solarisbay/commit/9cfbf9934c208d5ccd5e12a974374de6c1395e71))
* **gatekeeper:** keep speaker-ID opt-in so code matches the template default ([e8de7c6](https://github.com/mdopp/solarisbay/commit/e8de7c6d84c05c18426ec9befd970afc20421b31))
* **gatekeeper:** match the ECAPA embedding dimension so speaker-ID can work ([2cc0493](https://github.com/mdopp/solarisbay/commit/2cc0493b2ab82a74060e728ad52d37aced587311))
* **gatekeeper:** match the ECAPA embedding dimension so speaker-ID can work ([dd97c47](https://github.com/mdopp/solarisbay/commit/dd97c4781cdfa49d9ea5c068a49a54a0c0c6e7f5))
* **gatekeeper:** wire the speaker-id env flag into the config ([bd37520](https://github.com/mdopp/solarisbay/commit/bd37520d1d40fff05d8f4e2e64e6d03577464fc8))
* **ha:** Remap defekter Cast-Gruppe media_player.wohnzimmer auf media_player.wohnzimmer_paar ([18ea6ab](https://github.com/mdopp/solarisbay/commit/18ea6ab79654fb721d0b7f3a15d61b54e56fb337))
* **ingest:** resolve paperless doc id from related_document_ids list ([d2955a7](https://github.com/mdopp/solarisbay/commit/d2955a7f9daf2f1e117fa3804ee3883cdce6399f))
* **ingest:** resolve paperless doc id from related_document_ids list ([6792b6e](https://github.com/mdopp/solarisbay/commit/6792b6e6600cfeccd0a6719a9a6860bdda9293e2)), closes [#1061](https://github.com/mdopp/solarisbay/issues/1061)
* **media:** 500-Utterances-Testsuite + Verb-/Particle-Stripping für 100% Trefferquote ([1eaa62f](https://github.com/mdopp/solarisbay/commit/1eaa62fd6de6dff8c30fe490091db0043547c4c1))
* **media:** Füllwörter ('Spielermusik', 'Spiele Musik') filtern ([7771003](https://github.com/mdopp/solarisbay/commit/77710033a6c5c811f92bd185c39243431a3a1c98))
* **media:** Raum-Suffixe ('im Wohnzimmer', 'in der Küche') aus Suchanfragen filtern ([0f3ad94](https://github.com/mdopp/solarisbay/commit/0f3ad94018df3417025306ed614c83b42e585f4b))
* **media:** robuste Radio-Erkennung + kurze Antwort bei play_radio ([35ced35](https://github.com/mdopp/solarisbay/commit/35ced35f9900832b9c31872e8d438ec40713261f))
* **media:** robuste Radio-Erkennung + kurze Antwort bei play_radio ([ba04f5b](https://github.com/mdopp/solarisbay/commit/ba04f5ba2ff7d6127693705574dd8a6d14e231d5))
* **media:** STT-Phonetik-Normalisierung ('Spiel da ins Live', 'einsleif') + Remapping in resolve_play_device ([44e7371](https://github.com/mdopp/solarisbay/commit/44e737118981df946b1a437f48642fa462f996e6))
* **radio:** Station Query Sanitizer in radio.py + Real-API-Resolution Fix ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([158efab](https://github.com/mdopp/solarisbay/commit/158efab57b9b6440c8138004258ca4099cb68eec))
* **stt:** Erzwinge deutsches Sprachmodell (language='de') in Whisper STT Client ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([9d676a8](https://github.com/mdopp/solarisbay/commit/9d676a89b8ae9ff5ddbc8752ab6dcbd738ec5e94))
* **template:** create paperless superuser when DRF token mint fails ([57d0370](https://github.com/mdopp/solarisbay/commit/57d03702247127970e6ae61555c0feb7126bd521)), closes [#1036](https://github.com/mdopp/solarisbay/issues/1036)
* **template:** give the wakeword trainer time for its first image pull ([d58c1bc](https://github.com/mdopp/solarisbay/commit/d58c1bc9f62825fd0fa85e0e8394120a283f0398))
* **template:** give the wakeword trainer time for its first image pull ([3843715](https://github.com/mdopp/solarisbay/commit/3843715d083cf74284d3ce29a692ddbe16b5c636))
* **template:** inject PAPERLESS_SECRET_KEY in paperless template ([#1030](https://github.com/mdopp/solarisbay/issues/1030)) ([ca13c46](https://github.com/mdopp/solarisbay/commit/ca13c46aab203c8555b8b169619675e6e9b95e72))
* **template:** let a deploy move the wakeword trainer onto the rebuilt image ([#1090](https://github.com/mdopp/solarisbay/issues/1090)) ([f52138f](https://github.com/mdopp/solarisbay/commit/f52138f463e68a8fa0d46bcc4990813c95ff5c7f))
* **template:** paperless timezone default + OCR mode for the beta image ([570f1c8](https://github.com/mdopp/solarisbay/commit/570f1c8d41c8f782a21585bce2f5c8f4aec90c6e))
* **template:** paperless timezone default + OCR mode for the beta image ([1d199a7](https://github.com/mdopp/solarisbay/commit/1d199a75eb38702afd272baa580bc540cc7528d2)), closes [#1031](https://github.com/mdopp/solarisbay/issues/1031)
* **template:** retry the trainer image identity reads through the deploy window ([#1100](https://github.com/mdopp/solarisbay/issues/1100)) ([8a205bc](https://github.com/mdopp/solarisbay/commit/8a205bc3bc0d0b897f9e381c805f6ad72446c885)), closes [#1092](https://github.com/mdopp/solarisbay/issues/1092)
* **template:** run the wakeword trainer image refresh after chat is back ([#1099](https://github.com/mdopp/solarisbay/issues/1099)) ([6974720](https://github.com/mdopp/solarisbay/commit/6974720b5fb10f4959278e3612f3202b76d2289e)), closes [#1092](https://github.com/mdopp/solarisbay/issues/1092)
* **voice:** close the enrol request when consent is refused ([5be1bb3](https://github.com/mdopp/solarisbay/commit/5be1bb3c35154425ab7c8f33941607c939bb22d4))
* **voice:** close the enrol request when consent is refused ([b3be32f](https://github.com/mdopp/solarisbay/commit/b3be32fff219942ce23619b3436fc3a87a8540bc))
* **voice:** drop the wake-word step and raise the profile to five sentences ([8dfaaf2](https://github.com/mdopp/solarisbay/commit/8dfaaf2d7a8b2518eceeadc6c8c8fd296f3a7218))
* **voice:** Enable Speaker-ID & Live Voice Enrollment by default in gatekeeper config ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([d7f05fa](https://github.com/mdopp/solarisbay/commit/d7f05fa9268d91fa041c43146e6a083e0aa8a925))
* **voice:** Enforce missing_uid prompt + 600s TTL for uninterrupted enrollment ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([bdb3b2b](https://github.com/mdopp/solarisbay/commit/bdb3b2b8af4b55422ba1a9e024124d6b48ecd806))
* **voice:** Enforce strict '?' question mark ending on all enrollment & wakeword voice responses ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([a0285c4](https://github.com/mdopp/solarisbay/commit/a0285c44966f7df62cb48a930b0f42f3d96c0f3b))
* **voice:** Fix FSM stream event format ('assistant.delta') to eliminate 'unable to read gate response' ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([f6eb960](https://github.com/mdopp/solarisbay/commit/f6eb960b3a453be1bc2e8027da5d5213a12b7016))
* **voice:** Fix sample deletion decrement & purge remaining M-D-O-P-P prompt examples ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([70dd377](https://github.com/mdopp/solarisbay/commit/70dd377b7b297538932980b2bfb368fab0591f3f))
* **voice:** Force ephemeral=True when client is solaris-enrollment ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([bf1a2f3](https://github.com/mdopp/solarisbay/commit/bf1a2f3ac0b04c54f9446d7322e82a5953772eaa))
* **voice:** hand enrolment to the wizard instead of running a second state machine ([304de6a](https://github.com/mdopp/solarisbay/commit/304de6aef27e96088ed89819d4c7c7753ba81788))
* **voice:** hand enrolment to the wizard instead of running a second state machine ([e13a7fa](https://github.com/mdopp/solarisbay/commit/e13a7fa117ea451216154d02fc0e2965c5c2bd84))
* **voice:** has_any_active_request blockiert HA-Tools systemweit waehrend Enrollment ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([79a9fd6](https://github.com/mdopp/solarisbay/commit/79a9fd600dec864e3f0a23ff797a825fccbb2285))
* **voice:** Implement 180s TTL auto-expiration check in FSM state machine ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([65ab2ff](https://github.com/mdopp/solarisbay/commit/65ab2ffda355049545fe874c8ee8e7aaf9434443))
* **voice:** Increment sample count deterministically on each FSM turn ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([7001fea](https://github.com/mdopp/solarisbay/commit/7001fea7267879ca4225d820cfe3a6a612091e83))
* **voice:** let a plain "nein" end the conversation ([6a16eee](https://github.com/mdopp/solarisbay/commit/6a16eee75bbea3248509b60e7e08949d0c64a2e2))
* **voice:** let a plain "nein" end the conversation ([ec9bc59](https://github.com/mdopp/solarisbay/commit/ec9bc59de8608a3ebd851e693260e45539768ad6))
* **voice:** let only the gatekeeper count a wakeword sample ([ab8b3e5](https://github.com/mdopp/solarisbay/commit/ab8b3e5d19f8d74b6b2a946ab75cae34108bedd5))
* **voice:** let only the gatekeeper count a wakeword sample ([15846be](https://github.com/mdopp/solarisbay/commit/15846bebbfd3bf33bf4235f319e871d4c92615b6))
* **voice:** Nutzerfreundliche Sprachausgabe für Qualitäts-Audit ('Bei deiner letzten Aufnahme', 'Bei der Probe von Marco') ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([38357be](https://github.com/mdopp/solarisbay/commit/38357be291ee94af26117645e6299d1ceb33baf8))
* **voice:** Remove all hardcoded mdopp/michael examples from tool prompt schemas ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([9872927](https://github.com/mdopp/solarisbay/commit/9872927cff782592c980e124973172a1f921042f))
* **voice:** report the real enrollment result instead of assuming success ([007cbe4](https://github.com/mdopp/solarisbay/commit/007cbe47ac106dd77cb0600d40e1a9ea6596adb5))
* **voice:** Require explicit name/spelled UID on consent-only turns ('ja') ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([42b302f](https://github.com/mdopp/solarisbay/commit/42b302fd7a860461d4530f82a0fb2e0e613da339))
* **voice:** Reset FSM state when user triggers a new enrollment flow ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([01e326f](https://github.com/mdopp/solarisbay/commit/01e326f68954bdf6959d7a7a6301e30561ea9b87))
* **voice:** Route all interactive & voice routes (/api/chat, /ollama/api/chat) to isolated solaris-enrollment gateway ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([d8de3fc](https://github.com/mdopp/solarisbay/commit/d8de3fc34f0533744299d3f44e292788bd212a06))
* **voice:** Say-Field Short-Circuit — bypass 2nd LLM pass for instant enrollment responses ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([d87c1ef](https://github.com/mdopp/solarisbay/commit/d87c1ef553ceb846307b1290f1b5f6b0b25f4820))
* **voice:** scope wakeword sample deletion to the resident who owns it ([a502d67](https://github.com/mdopp/solarisbay/commit/a502d67813347bfcaa98551d628add3a598f4a52))
* **voice:** scope wakeword sample deletion to the resident who owns it ([006d9a7](https://github.com/mdopp/solarisbay/commit/006d9a7c4279b3f0a0a4595a63a50ebad5f7a442))
* **voice:** stop claiming wakeword training started when nothing ran ([67e7716](https://github.com/mdopp/solarisbay/commit/67e77169b381f2d7e6d7ad8127459bc39f2a7ebd))
* **voice:** stop stale state from hijacking prompts, routing and punctuation ([12c113c](https://github.com/mdopp/solarisbay/commit/12c113c15c3d1c04354c089314e0ada1489a6da5))
* **voice:** stop stale state from hijacking prompts, routing and punctuation ([74f7f8e](https://github.com/mdopp/solarisbay/commit/74f7f8e08adbcaa5e91693a0831c12ba44286767))
* **voice:** stop turning enrolment phrases into residents ([1dcbb6e](https://github.com/mdopp/solarisbay/commit/1dcbb6e5293644035349d04b1bd76c59109ef991))
* **voice:** stop turning enrolment phrases into residents ([acc5321](https://github.com/mdopp/solarisbay/commit/acc53217ef0291b52e404a8c9f683930d04a0cad))
* **voice:** Stricktes Fragezeichen-Erzwingen ('Was ist dein zweiter Satz?') für durchgehende Voice PE Pipeline ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([6e62571](https://github.com/mdopp/solarisbay/commit/6e62571b1e948e8925438da18289d9e06d5a81ef))
* **voice:** Strict Verbatim Voice Response Steering & Clear 2-Step Enrollment UX ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([651d348](https://github.com/mdopp/solarisbay/commit/651d3482ebb8fa3992e2ee26ebd8b1f77368dce2))
* **voice:** Strip HA history array for solaris-enrollment turns to prevent name leak ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([772b905](https://github.com/mdopp/solarisbay/commit/772b90592dd11308dbf73e93d86e4ea0a85a73ff))
* **voice:** STT Phonetik-Aliases ('Wake World', 'Breakwater') für start_wakeword_enrollment ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([165f34c](https://github.com/mdopp/solarisbay/commit/165f34cb5a06c5766c2ab26d93d51fe37ac52e76))
* **voice:** Toolbox-Isolierung während aktivem Enrollment (0.1s Antwortzeit & Schutz vor HA-Geräteaktionen) ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([4288ff4](https://github.com/mdopp/solarisbay/commit/4288ff42dbfce6e380f36bbd03d6ae82c741d93e))
* **voice:** Voice Assist Pipeline Fragezeichen-Fix ('Was ist dein erster Satz?') + Kürzel-Abfrage ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([40746e1](https://github.com/mdopp/solarisbay/commit/40746e15a5704bb52d16814d097a3febfbe231a3))
* wakeword training dedupe, trainer image restart, SSE heartbeat, and ha_watch card throttling ([#1097](https://github.com/mdopp/solarisbay/issues/1097)) ([f80b3b4](https://github.com/mdopp/solarisbay/commit/f80b3b407f9283b0be2f3bf38f59472ba4b41916))
* **wakeword-trainer:** Keep pip's cache on the work volume ([7c632c9](https://github.com/mdopp/solarisbay/commit/7c632c96e43419ba6baaccee820da5c82ae5115b)), closes [#1066](https://github.com/mdopp/solarisbay/issues/1066)


### Performance Improvements

* **voice:** Add lightweight 50-token prompt for solaris-enrollment to eliminate 15s prefill latency ([#1056](https://github.com/mdopp/solarisbay/issues/1056)) ([e694468](https://github.com/mdopp/solarisbay/commit/e6944680e3e1d008c73ce28d6a6de79bcb80a0f3))


### Reverts

* **template:** drop the wakeword trainer image refresh from post-deploy ([#1098](https://github.com/mdopp/solarisbay/issues/1098)) ([c9df6c1](https://github.com/mdopp/solarisbay/commit/c9df6c15c57c4df6587c8a53047937165d78a17a))

## [0.35.0](https://github.com/mdopp/solarisbay/compare/v0.34.0...v0.35.0) (2026-07-22)


### Features

* activate per-resident calendar under the solaris principal ([#1012](https://github.com/mdopp/solarisbay/issues/1012)) ([ce9e421](https://github.com/mdopp/solarisbay/commit/ce9e42185878fc3b7d9d6b10dd30cc61ba48113e))
* **chat:** .doc &lt;text&gt; search/filter existing documents in the dot-command card ([d6438a5](https://github.com/mdopp/solarisbay/commit/d6438a5255973b96e457b23b53f21a887c407742)), closes [#970](https://github.com/mdopp/solarisbay/issues/970)
* **chat:** .doc sorts the upload right away — category + keywords, no dead-end ([#960](https://github.com/mdopp/solarisbay/issues/960)) ([8d93305](https://github.com/mdopp/solarisbay/commit/8d933058a681f9f2d3fd8084339c509668cdeab9))
* **chat:** .photo dot-command — upload to Immich + search by person/text ([e5ca54f](https://github.com/mdopp/solarisbay/commit/e5ca54f18fc14e2aa9352b2c5cd4a3051856ad78)), closes [#961](https://github.com/mdopp/solarisbay/issues/961)
* **chat:** .task list/filter — the argument creates AND filters (the pattern) ([#955](https://github.com/mdopp/solarisbay/issues/955)) ([2c88f55](https://github.com/mdopp/solarisbay/commit/2c88f55fe1251dc43ab9b2b417d9efbc4483e71c))
* **chat:** .task recognises an inline date in TT.MM.JJJJ and lifts it to due ([#954](https://github.com/mdopp/solarisbay/issues/954)) ([60ebc5f](https://github.com/mdopp/solarisbay/commit/60ebc5f42e1bcc6e610d36d4bb484e2b1a287fe6))
* **chat:** /help explains Solaris (intent + / vs .), drop the .help dot-command ([#963](https://github.com/mdopp/solarisbay/issues/963)) ([35c95a2](https://github.com/mdopp/solarisbay/commit/35c95a232bec07294647d191b3126903b8a69723))
* **chat:** add .home dot-command to filter and control devices ([#981](https://github.com/mdopp/solarisbay/issues/981)) ([66f35b8](https://github.com/mdopp/solarisbay/commit/66f35b84621c9be4440dc53498aa659d45f9fe6a))
* **chat:** Aufgaben doorway count badge + composer double-Esc clear ([f22ac07](https://github.com/mdopp/solarisbay/commit/f22ac075bc91d380c2087d79ca43792db96d5f01)), closes [#969](https://github.com/mdopp/solarisbay/issues/969) [#971](https://github.com/mdopp/solarisbay/issues/971)
* **chat:** Aufgaben to-do surface — task entity, chat tools, doorway, calendar ([#951](https://github.com/mdopp/solarisbay/issues/951)) ([fe13005](https://github.com/mdopp/solarisbay/commit/fe13005fcbc1156b1ef1af4a4149664e0370512b))
* **chat:** dedupe provider contacts by normalized name ([#923](https://github.com/mdopp/solarisbay/issues/923)) ([4b82cf5](https://github.com/mdopp/solarisbay/commit/4b82cf5136f489d9f714dfa318f0c4210ee08f82))
* **chat:** device cards in the Solaris Android-widget style (usable garage) ([#975](https://github.com/mdopp/solarisbay/issues/975)) ([51df097](https://github.com/mdopp/solarisbay/commit/51df09786b78a33606c90fe45dc960ddb60c006a))
* **chat:** dot-command .contacts — create a contact (name/email/phone) + filter ([#964](https://github.com/mdopp/solarisbay/issues/964)) ([48f975c](https://github.com/mdopp/solarisbay/commit/48f975c26656e8e7a7044ad752698695753b8478))
* **chat:** dot-command .doc (upload) + label the families /control · .tool ([#958](https://github.com/mdopp/solarisbay/issues/958)) ([c9488b4](https://github.com/mdopp/solarisbay/commit/c9488b46cbb63b4c28296e1b121d10a9236007ea))
* **chat:** dot-command .note — create a quick note + live-search notes ([#956](https://github.com/mdopp/solarisbay/issues/956)) ([3c63b47](https://github.com/mdopp/solarisbay/commit/3c63b47aa17ca49cd2eedf961735b474ff432ad6))
* **chat:** dot-commands slice A — .help and .task with a live card in chat ([#953](https://github.com/mdopp/solarisbay/issues/953)) ([43d2c9c](https://github.com/mdopp/solarisbay/commit/43d2c9c999f3792fb498a6fd8647ff54f9a36d91))
* **chat:** edit .tool items on click — the create·find·edit third leg ([9985cfd](https://github.com/mdopp/solarisbay/commit/9985cfdd4625725404d54711835dde5f2c176e68)), closes [#967](https://github.com/mdopp/solarisbay/issues/967)
* **chat:** favourite ★/☆ toggle on cards + .note clickable/deduped + dot-command wiring guards ([#990](https://github.com/mdopp/solarisbay/issues/990)) ([156daab](https://github.com/mdopp/solarisbay/commit/156daab00e921bd686cbd0174ab02b1d5727ccda))
* **chat:** generalize live-update engine to a registry of hosts ([#986](https://github.com/mdopp/solarisbay/issues/986)) ([2220067](https://github.com/mdopp/solarisbay/commit/2220067a367022518027fa0bdfb33818c7e0d46a))
* **chat:** Geräte page (was Favoriten) + remove energy page + colour swatch left ([#987](https://github.com/mdopp/solarisbay/issues/987)) ([5c22670](https://github.com/mdopp/solarisbay/commit/5c22670b19f816ec1a34ea48c825f95389686895))
* **chat:** light card colour follows on/off and inline colour button ([#985](https://github.com/mdopp/solarisbay/issues/985)) ([f79594b](https://github.com/mdopp/solarisbay/commit/f79594b6ff0848fd5b88911df12755c30b0c5550))
* **chat:** music/radio favorite cards show what/where/volume ([7965f28](https://github.com/mdopp/solarisbay/commit/7965f28dbf2ea2bfafe9c5385c4501bb0630df6a)), closes [#972](https://github.com/mdopp/solarisbay/issues/972)
* **chat:** name the two command families — /=Chat & Solaris, .=Erfassen & Finden ([#957](https://github.com/mdopp/solarisbay/issues/957)) ([b9bd155](https://github.com/mdopp/solarisbay/commit/b9bd155ffb6513d093f4e258de3bad450e5eb795))
* **chat:** people-surfaces read person entities + resolve aliases ([#1000](https://github.com/mdopp/solarisbay/issues/1000)) ([34cdce2](https://github.com/mdopp/solarisbay/commit/34cdce2305b7885cb0030d42f6eb956d55688637))
* **chat:** per-resident calendar under the resident principal (option A, /&lt;resident&gt;/solaris/) ([#1015](https://github.com/mdopp/solarisbay/issues/1015)) ([a6c55c5](https://github.com/mdopp/solarisbay/commit/a6c55c54c7c1d243873114a36ec88fc5d89ba330))
* **chat:** project document deadlines into a Solaris Fristen calendar ([#925](https://github.com/mdopp/solarisbay/issues/925)) ([e757758](https://github.com/mdopp/solarisbay/commit/e7577582263238aa70873619a9f18452971c3459))
* **chat:** providers become phone-book contacts, 300dpi table vision ([#919](https://github.com/mdopp/solarisbay/issues/919)) ([3799a5b](https://github.com/mdopp/solarisbay/commit/3799a5ba0fa33de7a438089e1a333d3a1a8c85a8))
* **chat:** route household calendar items to the primary resident ([#1018](https://github.com/mdopp/solarisbay/issues/1018)) ([01fec71](https://github.com/mdopp/solarisbay/commit/01fec712beac864a48fa6316c0b168549389c190))
* **chat:** split .energy out of .home into its own dot-command ([#983](https://github.com/mdopp/solarisbay/issues/983)) ([f5875f2](https://github.com/mdopp/solarisbay/commit/f5875f2b2c75c5f30468d1264a15c92728286038))
* **chat:** start-page + dot-command batch — climate stepper, Aufgaben door, .doc/.tool/.photo, richer favorites ([7fee0d3](https://github.com/mdopp/solarisbay/commit/7fee0d3e2dc716fd3e417ad97facf7d93f19ef84))
* **chat:** sync provider contacts into the phone book via Radicale ([#924](https://github.com/mdopp/solarisbay/issues/924)) ([9ebfeda](https://github.com/mdopp/solarisbay/commit/9ebfedaaf988b139bc92846deb125afcbfe38d8f))
* **chat:** write per-resident calendars in the deadlines sync ([#1009](https://github.com/mdopp/solarisbay/issues/1009)) ([fd32631](https://github.com/mdopp/solarisbay/commit/fd3263194937776ddd37f4e67699927e70905f2a))
* **solaris:** wire CalDAV read ingest and parse .contacts into structured fields ([#1020](https://github.com/mdopp/solarisbay/issues/1020)) ([c540378](https://github.com/mdopp/solarisbay/commit/c5403788064d0b275409b5f345f381240a8aa6fc))
* **template:** converge Radicale rights so solaris writes /&lt;resident&gt;/solaris/ ([#1016](https://github.com/mdopp/solarisbay/issues/1016)) ([cc06d93](https://github.com/mdopp/solarisbay/commit/cc06d93f558cda0ba2263865b308ebc4717b19bc))


### Bug Fixes

* **chat:** .task checkbox rows + open/last-week filter, restrictive .note search ([#959](https://github.com/mdopp/solarisbay/issues/959)) ([bd86f7f](https://github.com/mdopp/solarisbay/commit/bd86f7fb5dfbc545d1ae24a5a0f9e34deb1049f7))
* **chat:** a running import beats a stale pending plan in import status ([#950](https://github.com/mdopp/solarisbay/issues/950)) ([472c445](https://github.com/mdopp/solarisbay/commit/472c4451ca7f773d4a3e28992d0af08512d2c130))
* **chat:** align command menu into two columns (command + description) ([#962](https://github.com/mdopp/solarisbay/issues/962)) ([e019063](https://github.com/mdopp/solarisbay/commit/e019063a0a3c3477369fc8d75e9e8ba95fc0dfe8))
* **chat:** classify music imports mechanically, drop the per-track LLM call ([#945](https://github.com/mdopp/solarisbay/issues/945)) ([efaa832](https://github.com/mdopp/solarisbay/commit/efaa832abcb36204dc5cfa5d9596dcb8d16644ba))
* **chat:** de-duplicate .note results by label, open the OKF note ([#991](https://github.com/mdopp/solarisbay/issues/991)) ([cac375f](https://github.com/mdopp/solarisbay/commit/cac375fa075b807ecf2ea5be5c1bdc127ac2db23))
* **chat:** directive task tool descriptions so Solaris adds to-dos, not asks ([#952](https://github.com/mdopp/solarisbay/issues/952)) ([c5f7c58](https://github.com/mdopp/solarisbay/commit/c5f7c580ea8f69fb52e07e7f9d4414f5e7dbbf01))
* **chat:** drop the inline skill aliases from the / menu (listed but broken) ([#965](https://github.com/mdopp/solarisbay/issues/965)) ([26c8a39](https://github.com/mdopp/solarisbay/commit/26c8a39ebd2f1a72ddbdf1fae0fa2f5d013f9b2e))
* **chat:** find YouTube watch-history by content, not the English filename ([#936](https://github.com/mdopp/solarisbay/issues/936)) ([3128143](https://github.com/mdopp/solarisbay/commit/3128143e0422165d13ef50ffb41ff9e4f83d1894)), closes [#935](https://github.com/mdopp/solarisbay/issues/935)
* **chat:** keep device-card SSE alive and reconcile on reconnect ([#988](https://github.com/mdopp/solarisbay/issues/988)) ([9981827](https://github.com/mdopp/solarisbay/commit/998182759d26dc94ca14986cf8df0da5a2b9a8c3))
* **chat:** make .home device cards fill width on mobile ([#982](https://github.com/mdopp/solarisbay/issues/982)) ([9fab639](https://github.com/mdopp/solarisbay/commit/9fab639cd8523f7c45071ba288e47876ee9cb697))
* **chat:** make the Takeout import plan durable + actionable inline ([#938](https://github.com/mdopp/solarisbay/issues/938)) ([1ddf08a](https://github.com/mdopp/solarisbay/commit/1ddf08a9e30702c69504904ced38ac3d86dbba5a))
* **chat:** one import plan card in chat, live progress, no redundant confirm ([#948](https://github.com/mdopp/solarisbay/issues/948)) ([0874b03](https://github.com/mdopp/solarisbay/commit/0874b03015b266f7db45b3652fa06e7d23405967))
* **chat:** paperclip attach icon and accept a bare Takeout .json upload ([#946](https://github.com/mdopp/solarisbay/issues/946)) ([396f744](https://github.com/mdopp/solarisbay/commit/396f7442f5efa96d735875df0551ab17a458a0cb))
* **chat:** pick watch-history by DOMINANT watches, not any watch URL ([#937](https://github.com/mdopp/solarisbay/issues/937)) ([457d2e4](https://github.com/mdopp/solarisbay/commit/457d2e4e837d1cc0609ebd6d6d16231fe9c2c64f))
* **chat:** render Takeout import plan card in the composer upload path ([#947](https://github.com/mdopp/solarisbay/issues/947)) ([516473b](https://github.com/mdopp/solarisbay/commit/516473bdf58fc5ad3770f0e35fc3fa690d77bce7))
* **chat:** restore climate setpoint control in the widget renderer ([8eb96bc](https://github.com/mdopp/solarisbay/commit/8eb96bc8b52130bdaa60815b98a7fc0bc32f6d10)), closes [#974](https://github.com/mdopp/solarisbay/issues/974)
* **chat:** resume all owners' import jobs on boot ([#942](https://github.com/mdopp/solarisbay/issues/942)) ([d011c37](https://github.com/mdopp/solarisbay/commit/d011c37caa3734c06efda44a46bed7900f0780e8)), closes [#941](https://github.com/mdopp/solarisbay/issues/941)
* **chat:** run music import from the unknown bucket too, not just at classify ([#949](https://github.com/mdopp/solarisbay/issues/949)) ([af0120d](https://github.com/mdopp/solarisbay/commit/af0120df567e0f195cc6b51c5972f850990be493))
* **chat:** show progress during music-import classification + memoize it ([#944](https://github.com/mdopp/solarisbay/issues/944)) ([6446de4](https://github.com/mdopp/solarisbay/commit/6446de40ba4ca72f218fd6d20abf2d3fedc1afc9))
* **chat:** stop classifying every document as insurance, add invoice/utility ([#922](https://github.com/mdopp/solarisbay/issues/922)) ([150b4a7](https://github.com/mdopp/solarisbay/commit/150b4a7ca0ce781aa1c857bc7e9d6806c5d39b6b))
* **chat:** surface YouTube-Music resolution progress on the import job ([#940](https://github.com/mdopp/solarisbay/issues/940)) ([8d7b533](https://github.com/mdopp/solarisbay/commit/8d7b5334a52907fd21627a7c1cbd61062934a7ec))
* **chat:** tidy .contacts edit form + de-duplicate email-as-name ([#989](https://github.com/mdopp/solarisbay/issues/989)) ([1081c77](https://github.com/mdopp/solarisbay/commit/1081c776cfdab18027299fb9294c362f4a352178))
* **engine:** scope deadlines_sync task query to household-only ([81a7ee9](https://github.com/mdopp/solarisbay/commit/81a7ee9f39834b98e55fbd435ce952ca6c713e28))
* **engine:** scope deadlines_sync task query to household-only ([662c223](https://github.com/mdopp/solarisbay/commit/662c22357b99df451eb4c1db25be6cc515b6851c)), closes [#978](https://github.com/mdopp/solarisbay/issues/978)
* **template:** post-deploy inserts SYNC_DAV env when the rendered pod lacks it ([#1014](https://github.com/mdopp/solarisbay/issues/1014)) ([3b94dd8](https://github.com/mdopp/solarisbay/commit/3b94dd8e5f1cc77b458a3dc3b57fd5929c9aa869))
* **template:** radicale owner rights need a capturing user group + content-heal ([#1017](https://github.com/mdopp/solarisbay/issues/1017)) ([23f7531](https://github.com/mdopp/solarisbay/commit/23f753153f618e2444d5ba5425da73cc8c66b281))
* **template:** stamp HOUSEHOLD_CALENDAR_UID into the pod via post-deploy ([#1019](https://github.com/mdopp/solarisbay/issues/1019)) ([eb849cd](https://github.com/mdopp/solarisbay/commit/eb849cdeef8d9623fe711bd583a66d6d9b33d7e0))

## [0.34.0](https://github.com/mdopp/solarisbay/compare/v0.33.1...v0.34.0) (2026-07-20)


### Features

* **chat:** vision document extraction + mobile document cards ([#917](https://github.com/mdopp/solarisbay/issues/917)) ([7a980cd](https://github.com/mdopp/solarisbay/commit/7a980cdc108ec462138341b0a97570cb9ea0c90a))

## [0.33.1](https://github.com/mdopp/solarisbay/compare/v0.33.0...v0.33.1) (2026-07-20)


### Bug Fixes

* **chat:** document-extractor observability + fewer model swaps ([#915](https://github.com/mdopp/solarisbay/issues/915)) ([ddb1068](https://github.com/mdopp/solarisbay/commit/ddb10683664d136b1f7cfc44defc2e9bd1188988))

## [0.33.0](https://github.com/mdopp/solarisbay/compare/v0.32.4...v0.33.0) (2026-07-20)


### Features

* **chat:** document dedup by source, finance fields, mobile card layout ([#913](https://github.com/mdopp/solarisbay/issues/913)) ([3196d56](https://github.com/mdopp/solarisbay/commit/3196d56e49fadfb021d75b1d015155cbc796a61e))

## [0.32.4](https://github.com/mdopp/solarisbay/compare/v0.32.3...v0.32.4) (2026-07-20)


### Bug Fixes

* **chat:** run document extraction on gemma4:12b, one doc per turn ([#911](https://github.com/mdopp/solarisbay/issues/911)) ([86c8fda](https://github.com/mdopp/solarisbay/commit/86c8fda1307f1270e70a351794577ba850c4bd59))

## [0.32.3](https://github.com/mdopp/solarisbay/compare/v0.32.2...v0.32.3) (2026-07-20)


### Bug Fixes

* **chat:** few-shot extractor prompt to fill document fields ([#909](https://github.com/mdopp/solarisbay/issues/909)) ([fec2361](https://github.com/mdopp/solarisbay/commit/fec23619cc9cbe9ac641224c1468c6b5291d5aeb))

## [0.32.2](https://github.com/mdopp/solarisbay/compare/v0.32.1...v0.32.2) (2026-07-20)


### Bug Fixes

* **chat:** project okf/documents notes the extractor writes ([#907](https://github.com/mdopp/solarisbay/issues/907)) ([2fcf00a](https://github.com/mdopp/solarisbay/commit/2fcf00aece9f948b454a56f4250b58f13c4090bd))

## [0.32.1](https://github.com/mdopp/solarisbay/compare/v0.32.0...v0.32.1) (2026-07-20)


### Bug Fixes

* **chat:** structured document_extract tool for reliable extraction ([#905](https://github.com/mdopp/solarisbay/issues/905)) ([8c9695e](https://github.com/mdopp/solarisbay/commit/8c9695e6c9847fdc18a1155b61f09bde050e56d3))

## [0.32.0](https://github.com/mdopp/solarisbay/compare/v0.31.1...v0.32.0) (2026-07-20)


### Features

* **chat:** structured document extraction — typed facts, category tables, corrections ([#903](https://github.com/mdopp/solarisbay/issues/903)) ([502d62e](https://github.com/mdopp/solarisbay/commit/502d62e48a49d53a9d7136b117e59842d8f2805e))

## [0.31.1](https://github.com/mdopp/solarisbay/compare/v0.31.0...v0.31.1) (2026-07-19)


### Bug Fixes

* **chat:** OCR orientation and garbled-text-layer fallback for uploads ([#901](https://github.com/mdopp/solarisbay/issues/901)) ([aa0709b](https://github.com/mdopp/solarisbay/commit/aa0709bd4db8d686cb4a58d28e480b09fe10a64a))

## [0.31.0](https://github.com/mdopp/solarisbay/compare/v0.30.1...v0.31.0) (2026-07-19)


### Features

* **chat:** extract text from uploads and link the original ([#899](https://github.com/mdopp/solarisbay/issues/899)) ([b741b83](https://github.com/mdopp/solarisbay/commit/b741b831a45a3a2de469f4780e6b92e5339aa06f))

## [0.30.1](https://github.com/mdopp/solarisbay/compare/v0.30.0...v0.30.1) (2026-07-19)


### Bug Fixes

* **chat:** clear FK children when pruning empty note shells ([#897](https://github.com/mdopp/solarisbay/issues/897)) ([865c0d2](https://github.com/mdopp/solarisbay/commit/865c0d287c6c1cacfc443469d3211408573febef))

## [0.30.0](https://github.com/mdopp/solarisbay/compare/v0.29.0...v0.30.0) (2026-07-19)


### Features

* **chat:** prune vault noise — projection-only photos/albums/bands + empty-shell guard + data-driven Notizen doorways ([#895](https://github.com/mdopp/solarisbay/issues/895)) ([b2c4311](https://github.com/mdopp/solarisbay/commit/b2c4311cbe4a4f7be04f9ddb2729f22128c2873f))

## [0.29.0](https://github.com/mdopp/solarisbay/compare/v0.28.4...v0.29.0) (2026-07-19)


### Features

* **chat:** focused guided Notizen page — Card 1 overview + explainer (Phase I) ([9cc118e](https://github.com/mdopp/solarisbay/commit/9cc118ede3dc00690fe71b98fb8e343cec49bd21))
* **chat:** focused guided Notizen page — Card 1 overview + explainer (Phase I) ([aa5179c](https://github.com/mdopp/solarisbay/commit/aa5179c6834d08f1f2aa07709aecbfc733902a7c))

## [0.28.4](https://github.com/mdopp/solarisbay/compare/v0.28.3...v0.28.4) (2026-07-19)


### Performance Improvements

* **chat:** serve Notizen overview + stats from solaris.db, not vault scans ([d020208](https://github.com/mdopp/solarisbay/commit/d02020861b391b551d7239e983cb39b9ca309b88))
* **chat:** serve Notizen overview + stats from solaris.db, not vault scans ([32ad57c](https://github.com/mdopp/solarisbay/commit/32ad57c7644dc97af73d3b6ae670dd5e5251616e))

## [0.28.3](https://github.com/mdopp/solarisbay/compare/v0.28.2...v0.28.3) (2026-07-19)


### Performance Improvements

* **chat:** index concepts(ref_id, ref_kind) ([f207fb4](https://github.com/mdopp/solarisbay/commit/f207fb4a0b6ae3b4ebd1cafbbc7893319b0f0477))
* **chat:** index concepts(ref_id, ref_kind) — the write path's last full scan ([a6c2e8a](https://github.com/mdopp/solarisbay/commit/a6c2e8a1a561dcc32d6faa0dee5ed9c6fad4d274))

## [0.28.2](https://github.com/mdopp/solarisbay/compare/v0.28.1...v0.28.2) (2026-07-19)


### Performance Improvements

* **chat:** index entity resolution to fix O(n^2) ingest ([56060b1](https://github.com/mdopp/solarisbay/commit/56060b1e4895f69f2b981ec9d668359444fb701b))
* **chat:** index entity resolution to fix O(n^2) ingest ([44a571c](https://github.com/mdopp/solarisbay/commit/44a571c8dfce75ccf52edf58851d11570bc0e60a))

## [0.28.1](https://github.com/mdopp/solarisbay/compare/v0.28.0...v0.28.1) (2026-07-19)


### Bug Fixes

* **chat:** prune sweeps orphaned song markdown files ([c958fa9](https://github.com/mdopp/solarisbay/commit/c958fa9a263f28ed4555b965d0b49f84854746b2))
* **chat:** prune sweeps orphaned song markdown, not just concept-linked ([fd9d247](https://github.com/mdopp/solarisbay/commit/fd9d2472ebeaeaf2c32cffc194960c33a314cfd7))

## [0.28.0](https://github.com/mdopp/solarisbay/compare/v0.27.0...v0.28.0) (2026-07-19)


### Features

* **chat:** externally-sourced songs are projection-only; keep lean album/artist markdown for RAG ([43e974e](https://github.com/mdopp/solarisbay/commit/43e974e7d53ad494fee529af667dbfed05a54895)), closes [#877](https://github.com/mdopp/solarisbay/issues/877)
* **chat:** Google-Takeout import UX — Notizen section + chat .zip flow ([e6286de](https://github.com/mdopp/solarisbay/commit/e6286defbf658c1edefb5e3726f8dcc0b426454a)), closes [#869](https://github.com/mdopp/solarisbay/issues/869)
* **chat:** interactive Google-Takeout import flow (music job + upload UX) — closes epic [#860](https://github.com/mdopp/solarisbay/issues/860) ([0317a35](https://github.com/mdopp/solarisbay/commit/0317a35207844be6fc960b2f24a38e7d5583e97d))
* **chat:** interactive Takeout import flow (upload → classify → card → job) ([0e2c8fe](https://github.com/mdopp/solarisbay/commit/0e2c8fe18d0817ab0a7f8ad521bd91071248961d))
* **chat:** make album a first-class knowledge entity with song/artist join facts ([b7c658c](https://github.com/mdopp/solarisbay/commit/b7c658cad65c70585a260003817a3bc0d159711d)), closes [#876](https://github.com/mdopp/solarisbay/issues/876)
* **chat:** music wishlist as a fact-query over album entities (replaces [#859](https://github.com/mdopp/solarisbay/issues/859) note) ([9a3672a](https://github.com/mdopp/solarisbay/commit/9a3672a259330a3d2707824f462a7f6cd9d10c6d)), closes [#879](https://github.com/mdopp/solarisbay/issues/879)
* **chat:** music/OKF substrate — album entity + source-tagged facts ([#873](https://github.com/mdopp/solarisbay/issues/873) P1+P2) ([8b8b7d0](https://github.com/mdopp/solarisbay/commit/8b8b7d00e5f764481960ef1bb4ac79c98c60455c))
* **chat:** physical-collection notes contribute owned_physical facts to album entities ([7485552](https://github.com/mdopp/solarisbay/commit/74855521ddd213dd7dc1146c08a51378ba006e9a)), closes [#880](https://github.com/mdopp/solarisbay/issues/880)
* **chat:** prune legacy per-song OKF markdown + embeddings to projection-only ([4612dfb](https://github.com/mdopp/solarisbay/commit/4612dfb50713d98d8d5358577fad40aa5e5e0cab)), closes [#878](https://github.com/mdopp/solarisbay/issues/878)
* **chat:** stenograph writes used_to_love music facts from conversations ([6c1f554](https://github.com/mdopp/solarisbay/commit/6c1f55495cfb635f5c37eb7d74eec17e54de7deb)), closes [#881](https://github.com/mdopp/solarisbay/issues/881)
* **chat:** YouTube-Music import job writes wishlist facts to album entities ([3760abf](https://github.com/mdopp/solarisbay/commit/3760abf7711d25a6d38d3cf741fadba19ce41192)), closes [#868](https://github.com/mdopp/solarisbay/issues/868)
* **engine:** calendar Takeout importer PUTs to Radicale via CalDAV ([8f3920d](https://github.com/mdopp/solarisbay/commit/8f3920d5fb5ad8b4812789b8a4ac79c3b9d16a15)), closes [#865](https://github.com/mdopp/solarisbay/issues/865)
* **engine:** contacts Takeout importer PUTs to Radicale via CardDAV ([4e03fb4](https://github.com/mdopp/solarisbay/commit/4e03fb4aee602f87b9ae7aa8865a4b0296650192)), closes [#866](https://github.com/mdopp/solarisbay/issues/866)
* **engine:** Google-Takeout calendar/contacts/keep importers + retire [#859](https://github.com/mdopp/solarisbay/issues/859) note-enrichment ([19d2e3e](https://github.com/mdopp/solarisbay/commit/19d2e3e6634bec522936ea5f4c39c59c05f17497))
* **engine:** keep Takeout importer writes Obsidian Markdown to the vault ([a761db7](https://github.com/mdopp/solarisbay/commit/a761db7263a2f9ee76f64e894cd03de287d91177)), closes [#867](https://github.com/mdopp/solarisbay/issues/867)
* import capability foundation (S1-S3) + notes-search skill fix ([547d844](https://github.com/mdopp/solarisbay/commit/547d844d3cbf58587fd944c2d5e95ad3f6a69c45))
* **solarisbay:** add durable engine_import_jobs store and runner ([c5c862d](https://github.com/mdopp/solarisbay/commit/c5c862d805bea51331f75680299e229f305c7fcf)), closes [#864](https://github.com/mdopp/solarisbay/issues/864)
* **solarisbay:** enrich music-wishlist notes from OKF in the night run ([20c0a6a](https://github.com/mdopp/solarisbay/commit/20c0a6a65b3378c72abc1466ac6cf6e809b276ef)), closes [#859](https://github.com/mdopp/solarisbay/issues/859)
* **solarisbay:** vendor Google-Takeout import core into the engine ([1605e7c](https://github.com/mdopp/solarisbay/commit/1605e7c98b606a53a8454ca0e03f45d7bf1b2911)), closes [#863](https://github.com/mdopp/solarisbay/issues/863)


### Bug Fixes

* **skill:** point notes-search at the notes_search tool ([273c58f](https://github.com/mdopp/solarisbay/commit/273c58f91a494792233ef1a73fa957e79a785db7)), closes [#862](https://github.com/mdopp/solarisbay/issues/862)

## [0.27.0](https://github.com/mdopp/solarisbay/compare/v0.26.0...v0.27.0) (2026-07-17)


### Features

* **portal:** /download → latest signed companion APK ([4d8b587](https://github.com/mdopp/solarisbay/commit/4d8b587871dda8ea30e0cd3697ae7ba1d91b02e7))
* **portal:** /download → latest signed companion APK (www.dopp.cloud/download) ([abcf4d6](https://github.com/mdopp/solarisbay/commit/abcf4d6b42be7158457143f6753cb53dbb87098d))
* **portal:** forward HA last_updated as updated_at_ms on card specs ([#850](https://github.com/mdopp/solarisbay/issues/850)) ([e534a41](https://github.com/mdopp/solarisbay/commit/e534a41ddb8701e8d6afd9047828dff3d4628d77))
* **portal:** forward HA last_updated as updated_at_ms on card specs ([#850](https://github.com/mdopp/solarisbay/issues/850)) ([76d510a](https://github.com/mdopp/solarisbay/commit/76d510a674d01fc9cd3e88fe70f7c49d7390b86d))
* **solaris:** make /download public — add it to CHAT_SUBDOMAIN authSkipPaths ([4175d9c](https://github.com/mdopp/solarisbay/commit/4175d9c23868ec9cc525a0da82c65dd0c857200b))
* **solaris:** make /download public (authSkipPaths) ([70f3563](https://github.com/mdopp/solarisbay/commit/70f35635007c3f1a6b8e89ee8a8aa45d490ff9dc))


### Bug Fixes

* **engine:** move ApprovalPoller and SbCompanionClient reads onto read_sb_token ([4019d04](https://github.com/mdopp/solarisbay/commit/4019d04ddea074323c90ddda6b51e0508c740be0))
* **engine:** move ApprovalPoller and SbCompanionClient reads onto read_sb_token ([ba93624](https://github.com/mdopp/solarisbay/commit/ba93624e9aadcad0848f1f068f9a697a8883278e))
* **post-deploy:** overwrite a stale read-token file, don't keep it ([#818](https://github.com/mdopp/solarisbay/issues/818)) ([f248cb4](https://github.com/mdopp/solarisbay/commit/f248cb48ed4c5eb66e747aa3b88468d8c8c35bdf))
* **post-deploy:** overwrite a stale read-token file, don't keep it ([#818](https://github.com/mdopp/solarisbay/issues/818)) ([74abf27](https://github.com/mdopp/solarisbay/commit/74abf27fa4318424315113e61db698c30e753208))

## [0.26.0](https://github.com/mdopp/solarisbay/compare/v0.25.1...v0.26.0) (2026-07-15)


### Features

* **chat:** token-authed /napi/push subscribe + push servicebay approvals ([8ffd8cd](https://github.com/mdopp/solarisbay/commit/8ffd8cdcd42725c8a19229401137e8684ba59286)), closes [#843](https://github.com/mdopp/solarisbay/issues/843)
* **chat:** token-authed /napi/push subscribe/unsubscribe + servicebay approval push ([76a4bf9](https://github.com/mdopp/solarisbay/commit/76a4bf9508088b8e4bc35cad60d88d824d877dfe))

## [0.25.1](https://github.com/mdopp/solarisbay/compare/v0.25.0...v0.25.1) (2026-07-15)


### Bug Fixes

* **template:** consume SB-injected SB_READ_TOKEN instead of self-minting ([cd89de4](https://github.com/mdopp/solarisbay/commit/cd89de42b370d44ba264cc01e86a2c9815eae252)), closes [#818](https://github.com/mdopp/solarisbay/issues/818)
* **template:** post-deploy writes the SB-provided SB_READ_TOKEN instead of self-minting ([b67fd60](https://github.com/mdopp/solarisbay/commit/b67fd60834f5949ba157fe5b2974fbcede9500b9))

## [0.25.0](https://github.com/mdopp/solarisbay/compare/v0.24.0...v0.25.0) (2026-07-15)


### Features

* **chat:** /napi/servicebay operate — start/stop/restart via lifecycle-scoped token ([97baebf](https://github.com/mdopp/solarisbay/commit/97baebfb720990210dde50a614512eb5588f1275))
* **chat:** /napi/servicebay operate — start/stop/restart via lifecycle-scoped token ([550f72f](https://github.com/mdopp/solarisbay/commit/550f72f49bf60450f111d8d990e507d2808ded1f)), closes [#827](https://github.com/mdopp/solarisbay/issues/827)


### Bug Fixes

* **chat:** consistent WAL + busy_timeout across solaris.db writers so ingest stops dropping rows on lock ([aef929a](https://github.com/mdopp/solarisbay/commit/aef929a78434b34015c3565035c9975fcde61ac3))
* **chat:** raise ingest write-path busy_timeout + WAL the FTS backfill so boots stop dropping songs ([5252d4e](https://github.com/mdopp/solarisbay/commit/5252d4e40eef20e0fbb03af7b6db3f0059c7ca48)), closes [#835](https://github.com/mdopp/solarisbay/issues/835)

## [0.24.0](https://github.com/mdopp/solarisbay/compare/v0.23.0...v0.24.0) (2026-07-15)


### Features

* **chat:** shard immich event notes by year with an idempotent runtime migration ([307e6c7](https://github.com/mdopp/solarisbay/commit/307e6c7ae709bf78c3264918adba18ea386214c7))
* **chat:** shard okf/events by year + one-time flat-note migration ([042a684](https://github.com/mdopp/solarisbay/commit/042a6845bbccf636107ee37a324859a657fa1c29)), closes [#830](https://github.com/mdopp/solarisbay/issues/830)

## [0.23.0](https://github.com/mdopp/solarisbay/compare/v0.22.1...v0.23.0) (2026-07-15)


### Features

* **chat:** notes_search FTS5 index so it covers the full ~99k-file vault ([3634f25](https://github.com/mdopp/solarisbay/commit/3634f25ced8a589bcff9ee9ef9f7186c7502a25f))
* **chat:** SQLite-FTS5 index over the notes vault for notes_search ([99bfeac](https://github.com/mdopp/solarisbay/commit/99bfeac112a93e4c3facdb9c25fa0a04055c0dc5)), closes [#830](https://github.com/mdopp/solarisbay/issues/830)
* **chat:** token-authed /napi/upload stores camera captures into the notes vault ([#828](https://github.com/mdopp/solarisbay/issues/828)) ([b4867c0](https://github.com/mdopp/solarisbay/commit/b4867c024f4af224271a0e0f86ef70748496fa74)), closes [#826](https://github.com/mdopp/solarisbay/issues/826)

## [0.22.1](https://github.com/mdopp/solarisbay/compare/v0.22.0...v0.22.1) (2026-07-14)


### Bug Fixes

* **chat:** non-expiring read token for the unattended SB pollers ([#824](https://github.com/mdopp/solarisbay/issues/824)) ([45d602e](https://github.com/mdopp/solarisbay/commit/45d602e2420b8f59b6e6cef9a595fb580afd3bfd)), closes [#818](https://github.com/mdopp/solarisbay/issues/818) [#799](https://github.com/mdopp/solarisbay/issues/799)

## [0.22.0](https://github.com/mdopp/solarisbay/compare/v0.21.0...v0.22.0) (2026-07-14)


### Features

* **chat:** /napi/portal/watch per-device native watch-set feeds ha_watch ([#814](https://github.com/mdopp/solarisbay/issues/814)) ([07a1923](https://github.com/mdopp/solarisbay/commit/07a19239edb6509ea8f0867f9972c488e86df3c5)), closes [#810](https://github.com/mdopp/solarisbay/issues/810)
* **chat:** Solaris BFF — aggregate ServiceBay reads + republish approval events under /napi ([#816](https://github.com/mdopp/solarisbay/issues/816)) ([5cc5a03](https://github.com/mdopp/solarisbay/commit/5cc5a03a8fb6c558b7eb8c36a1eef475082ae2fc))
* **chat:** Solaris BFF approve/reject to ServiceBay via session-mint delegation ([#817](https://github.com/mdopp/solarisbay/issues/817)) ([4e9457b](https://github.com/mdopp/solarisbay/commit/4e9457b92e77f55df273f1ba56aece43f5773e1c)), closes [#811](https://github.com/mdopp/solarisbay/issues/811)


### Bug Fixes

* **chat:** BFF forwards the admin authelia_session cookie to the www mint host ([#821](https://github.com/mdopp/solarisbay/issues/821)) ([b34ec02](https://github.com/mdopp/solarisbay/commit/b34ec02e02e1d547b71cfefe53284156fd924316)), closes [#820](https://github.com/mdopp/solarisbay/issues/820)
* **chat:** route the delegated-admin mint through NPM to pass SB CSRF gate ([#819](https://github.com/mdopp/solarisbay/issues/819)) ([c023c74](https://github.com/mdopp/solarisbay/commit/c023c7484aaef81cc3052bbe1abc0fe99141171d)), closes [#811](https://github.com/mdopp/solarisbay/issues/811)

## [0.21.0](https://github.com/mdopp/solarisbay/compare/v0.20.0...v0.21.0) (2026-07-13)


### Features

* **chat:** /napi/portal/cameras — camera list for the Android widget picker — closes [#779](https://github.com/mdopp/solarisbay/issues/779) ([#780](https://github.com/mdopp/solarisbay/issues/780)) ([3cc6026](https://github.com/mdopp/solarisbay/commit/3cc60261facd1c8a70417658e19d3c5eaef1980f))
* **chat:** action-card type with callback endpoint and confirm-gate ([8ef17bb](https://github.com/mdopp/solarisbay/commit/8ef17bb30e5e2840ce411c5bf121bd301e2409d5))
* **chat:** action-card type with callback endpoint and confirm-gate ([83c40c5](https://github.com/mdopp/solarisbay/commit/83c40c59c3e0d785bb832cf14019dbb5b354f9dc)), closes [#787](https://github.com/mdopp/solarisbay/issues/787)
* **chat:** approval-gated delete/exec via one-shot elevated SB-MCP scope ([#807](https://github.com/mdopp/solarisbay/issues/807)) ([d6ea820](https://github.com/mdopp/solarisbay/commit/d6ea8200dca9dd20ecf660fd46c7f95026bc5eb6)), closes [#789](https://github.com/mdopp/solarisbay/issues/789)
* **chat:** expose card_state SSE stream under /napi/portal/events ([#808](https://github.com/mdopp/solarisbay/issues/808)) ([f5ed82a](https://github.com/mdopp/solarisbay/commit/f5ed82a8729a03f7c831341648bc93d41c4e9b95)), closes [#806](https://github.com/mdopp/solarisbay/issues/806)
* **chat:** pinned admin-only Wartung ops chat with SB-MCP at default scope ([0d6dd18](https://github.com/mdopp/solarisbay/commit/0d6dd18ab202af9507645d9518740687ead167a3)), closes [#786](https://github.com/mdopp/solarisbay/issues/786)
* **chat:** PWA camera-view route + shared Solar figure wordmark ([d3c52d7](https://github.com/mdopp/solarisbay/commit/d3c52d7c20ee85cbc0bd1aba2bae5780b5c6e093))
* **chat:** PWA camera-view route + shared Solar[figure]s wordmark ([cf4aff1](https://github.com/mdopp/solarisbay/commit/cf4aff12895a93c068c9d1b28fc908439462b37a)), closes [#782](https://github.com/mdopp/solarisbay/issues/782) [#783](https://github.com/mdopp/solarisbay/issues/783)
* **chat:** server-initiated message/card injection into a chat + push ([29b7fca](https://github.com/mdopp/solarisbay/commit/29b7fca29a32d636ab56e01a87fefcb72ca760b6)), closes [#785](https://github.com/mdopp/solarisbay/issues/785)
* **chat:** Wartung incoming approval-request cards + verdict callback ([#800](https://github.com/mdopp/solarisbay/issues/800)) ([540a1fe](https://github.com/mdopp/solarisbay/commit/540a1feb8e9f7d23c992614d0a6de3d94f530bba)), closes [#790](https://github.com/mdopp/solarisbay/issues/790)
* **chat:** Wartung phase 1 — server-initiated injection + pinned admin ops chat ([7b32747](https://github.com/mdopp/solarisbay/commit/7b32747c056d9cf6afcd4f127777c87967f544d4))
* **chat:** Wartung update-notification cards + admin SB-MCP deploy action ([#799](https://github.com/mdopp/solarisbay/issues/799)) ([ef0cd83](https://github.com/mdopp/solarisbay/commit/ef0cd8383bc192daff0e7100fe83d5101e6c04ed)), closes [#788](https://github.com/mdopp/solarisbay/issues/788)


### Bug Fixes

* **chat:** action_callback enforces admin on admin-only handlers ([#797](https://github.com/mdopp/solarisbay/issues/797)) ([98ad8ba](https://github.com/mdopp/solarisbay/commit/98ad8bae63f04dd4ea99d82333f31c952eb0894f)), closes [#796](https://github.com/mdopp/solarisbay/issues/796)
* **chat:** admin SB-MCP token via Authelia-session exchange ([#798](https://github.com/mdopp/solarisbay/issues/798)) ([8eab5b2](https://github.com/mdopp/solarisbay/commit/8eab5b20f758e621dba3d9fde6c1fc0d4cbd25d2)), closes [#794](https://github.com/mdopp/solarisbay/issues/794)
* **chat:** convert PEM VAPID_PRIVATE_KEY to raw scalar for pywebpush ([#805](https://github.com/mdopp/solarisbay/issues/805)) ([932633d](https://github.com/mdopp/solarisbay/commit/932633d86934309e54ed0eea24130e7791642ca8)), closes [#804](https://github.com/mdopp/solarisbay/issues/804)
* **chat:** derive VAPID_PUBLIC_KEY from the private key when unset ([#802](https://github.com/mdopp/solarisbay/issues/802)) ([234c8e6](https://github.com/mdopp/solarisbay/commit/234c8e6a3a5115881a9e59f2aec8fc46f351fe9e)), closes [#801](https://github.com/mdopp/solarisbay/issues/801)
* **chat:** handle PEM-format VAPID_PRIVATE_KEY in derive path ([#801](https://github.com/mdopp/solarisbay/issues/801) gap) ([#803](https://github.com/mdopp/solarisbay/issues/803)) ([f3facf4](https://github.com/mdopp/solarisbay/commit/f3facf4116f2b76d877411c66f2671d3c70c2a93))

## [0.20.0](https://github.com/mdopp/solarisbay/compare/v0.19.0...v0.20.0) (2026-07-12)


### Features

* **chat:** /napi camera snapshot endpoint for the Android camera widget — closes [#770](https://github.com/mdopp/solarisbay/issues/770) ([#771](https://github.com/mdopp/solarisbay/issues/771)) ([d405f74](https://github.com/mdopp/solarisbay/commit/d405f74989268992f1347288c522be3889936420))
* **chat:** /napi/portal/active — active-devices collection for the Android widget, no N+1 — closes [#773](https://github.com/mdopp/solarisbay/issues/773) ([#776](https://github.com/mdopp/solarisbay/issues/776)) ([6e536dd](https://github.com/mdopp/solarisbay/commit/6e536dd00672198a76dc0e056ff024d588ee1067))
* **chat:** add addable/state/energy endpoints to /napi for the Android widgets — closes [#762](https://github.com/mdopp/solarisbay/issues/762) ([#763](https://github.com/mdopp/solarisbay/issues/763)) ([252ea72](https://github.com/mdopp/solarisbay/commit/252ea72e8edfa641503e4cf1a57186e063efa532))
* **chat:** adopt the Solaris app icon as the shared PWA brand mark — closes [#768](https://github.com/mdopp/solarisbay/issues/768) ([#772](https://github.com/mdopp/solarisbay/issues/772)) ([4e47b4a](https://github.com/mdopp/solarisbay/commit/4e47b4a59965c34681804432d4ac00d4129282e4))
* **chat:** mobile Energie tab + ?ask household deep-link + single-device route ([#774](https://github.com/mdopp/solarisbay/issues/774)) ([e640085](https://github.com/mdopp/solarisbay/commit/e64008579d5656619a36c807941fc8e05b89d307))
* **chat:** Solaris brand over the Chats overlay + version badge moved to the footer ([#778](https://github.com/mdopp/solarisbay/issues/778)) ([195d864](https://github.com/mdopp/solarisbay/commit/195d86436cafc61f536265898841c8e5d1d004b3))


### Bug Fixes

* **chat:** use the actual Solaris figure for the PWA brand mark ([#775](https://github.com/mdopp/solarisbay/issues/775)) ([22fbceb](https://github.com/mdopp/solarisbay/commit/22fbceb2d3205e4e8cbdd84dd1e6e39a180e9dfe))

## [0.19.0](https://github.com/mdopp/solarisbay/compare/v0.18.0...v0.19.0) (2026-07-12)


### Features

* **chat:** /napi native-API prefix — strict device-token auth for Android widgets, fail-closed — closes [#757](https://github.com/mdopp/solarisbay/issues/757) ([#760](https://github.com/mdopp/solarisbay/issues/760)) ([4e88545](https://github.com/mdopp/solarisbay/commit/4e885454d11bfcebc6621ddc5e054f05eb73ae5a))
* **chat:** /pair-device page — mint a device-token + deep-link handoff to the Android app — closes [#751](https://github.com/mdopp/solarisbay/issues/751) ([#752](https://github.com/mdopp/solarisbay/issues/752)) ([784971b](https://github.com/mdopp/solarisbay/commit/784971ba26da96c790598d6bfa531764be69e8ba))
* **chat:** authSkipPaths for /.well-known/ + /static/ (servicebay[#2210](https://github.com/mdopp/solarisbay/issues/2210)) ([d236c55](https://github.com/mdopp/solarisbay/commit/d236c555357ed3b4239fb711319eb14c9e14097f))
* **chat:** authSkipPaths for /.well-known/ + /static/ (servicebay[#2210](https://github.com/mdopp/solarisbay/issues/2210)) ([9745423](https://github.com/mdopp/solarisbay/commit/97454237fd246a089d4818aa3fe76a0f6b9d55cf))
* **chat:** clean Haushalt vs Meine Favoriten — display-dedup preferring personal, scope choice + move, collapse when uid==household — closes [#745](https://github.com/mdopp/solarisbay/issues/745) ([#746](https://github.com/mdopp/solarisbay/issues/746)) ([0cc15ce](https://github.com/mdopp/solarisbay/commit/0cc15ce9d148949e0dde0d6fff3f57648d806300))
* **chat:** colour picker previews live, reverts on cancel, keeps on confirm — closes [#738](https://github.com/mdopp/solarisbay/issues/738) ([#739](https://github.com/mdopp/solarisbay/issues/739)) ([4c2a85c](https://github.com/mdopp/solarisbay/commit/4c2a85c85709611d96b60599529e45089e589bd1))
* **chat:** device-token auth for native Android clients [#717](https://github.com/mdopp/solarisbay/issues/717) ([#748](https://github.com/mdopp/solarisbay/issues/748)) ([67db18c](https://github.com/mdopp/solarisbay/commit/67db18c0e51d688fe9059128d86169de9f359272))
* **chat:** richer card-spec + per-entity history endpoint for native widgets — closes [#754](https://github.com/mdopp/solarisbay/issues/754) [#755](https://github.com/mdopp/solarisbay/issues/755) ([#756](https://github.com/mdopp/solarisbay/issues/756)) ([cce43d2](https://github.com/mdopp/solarisbay/commit/cce43d2b92297b38d66a6a3fdc12b8cccbce73d0))
* **chat:** serve /.well-known/assetlinks.json + real 192/512/maskable PWA icons for the Android TWA — part of [#716](https://github.com/mdopp/solarisbay/issues/716) ([#747](https://github.com/mdopp/solarisbay/issues/747)) ([ab512d0](https://github.com/mdopp/solarisbay/commit/ab512d08bd78139b5e4abc6ccfdde66f8a1eeb7f))
* **chat:** show a Home-Assistant-unreachable notice + unavailable cards on the start page ([#731](https://github.com/mdopp/solarisbay/issues/731)) ([b80f7b0](https://github.com/mdopp/solarisbay/commit/b80f7b04fa6e4904a043c84a48a5dad8af162495)), closes [#729](https://github.com/mdopp/solarisbay/issues/729)


### Bug Fixes

* **chat:** calmer card toggle, bigger touch switch, colour-picker no longer toggles the light ([#727](https://github.com/mdopp/solarisbay/issues/727)) ([1e49c74](https://github.com/mdopp/solarisbay/commit/1e49c7451ae8cbb96478ff35b00f5b28e13eafb4)), closes [#726](https://github.com/mdopp/solarisbay/issues/726)
* **chat:** confirm-gate keys on entity real domain not model routing ([#664](https://github.com/mdopp/solarisbay/issues/664)) ([11543ae](https://github.com/mdopp/solarisbay/commit/11543ae8bf9cfb33f83997f97137824713399556)), closes [#632](https://github.com/mdopp/solarisbay/issues/632)
* **chat:** drop redundant proxy_http_version from SSE tuning ([f0c9f67](https://github.com/mdopp/solarisbay/commit/f0c9f6736c51ea8dfd8460bf3a80a92c4a329279))
* **chat:** drop redundant proxy_http_version from SSE tuning ([fa525b2](https://github.com/mdopp/solarisbay/commit/fa525b2a3eada0045e485cfe7bcfac9534398b9d))
* **chat:** keep the colour-picker overlay open — suspend live card re-render while picking — closes [#736](https://github.com/mdopp/solarisbay/issues/736) ([#737](https://github.com/mdopp/solarisbay/issues/737)) ([d8a7884](https://github.com/mdopp/solarisbay/commit/d8a7884cb3a3c26d3d80c0f4046e7d68d4215055))
* **chat:** light-off toggle settles once + show unavailable devices as inactive ([#734](https://github.com/mdopp/solarisbay/issues/734)) ([24b40c5](https://github.com/mdopp/solarisbay/commit/24b40c5d1e3365f6f96354c8e47e9a72b9a8e4f1)), closes [#732](https://github.com/mdopp/solarisbay/issues/732)
* **chat:** off light shows last-known brightness, not a fake 100% — closes [#733](https://github.com/mdopp/solarisbay/issues/733) ([#735](https://github.com/mdopp/solarisbay/issues/735)) ([9962d64](https://github.com/mdopp/solarisbay/commit/9962d644daa00bc76f3cbf0fbc84c6f4686f6d77))
* **chat:** pin a frequent device as its card, dedup favorites — closes [#743](https://github.com/mdopp/solarisbay/issues/743) ([#744](https://github.com/mdopp/solarisbay/issues/744)) ([79c6213](https://github.com/mdopp/solarisbay/commit/79c6213ea95539cda9738b502362a85fc681a94e))
* **chat:** readable names + actions in 'Häufig genutzt' instead of entity-id slugs ([#742](https://github.com/mdopp/solarisbay/issues/742)) ([55d28a7](https://github.com/mdopp/solarisbay/commit/55d28a7c4230860d349038de18b42ae19b2ebaf2)), closes [#741](https://github.com/mdopp/solarisbay/issues/741)
* **chat:** real apple-touch-icon rendered from the Solaris mark ([#730](https://github.com/mdopp/solarisbay/issues/730)) ([5ffb34c](https://github.com/mdopp/solarisbay/commit/5ffb34cccfecbae3a933f7fe4f539f03ba60b67a))
* **template:** persist ANDROID_CERT_FINGERPRINTS as a template default ([#759](https://github.com/mdopp/solarisbay/issues/759)) ([bcd6779](https://github.com/mdopp/solarisbay/commit/bcd67792bb16795af6cc17fcce5a5788e1db9d6e))
* **template:** re-assert HA ollama integration api_key on deploy so voice self-heals — closes [#557](https://github.com/mdopp/solarisbay/issues/557) ([#753](https://github.com/mdopp/solarisbay/issues/753)) ([e738ae6](https://github.com/mdopp/solarisbay/commit/e738ae6ec49af1fef177067e2c3c2c0b4f0f6aa4))

## [0.18.0](https://github.com/mdopp/solarisbay/compare/v0.17.0...v0.18.0) (2026-07-11)


### Features

* **chat:** add 180x180 apple-touch-icon.png for iOS home-screen ([0ebd2b2](https://github.com/mdopp/solarisbay/commit/0ebd2b29af1faef1b4b8215b14ae4bb3c76a5af1)), closes [#663](https://github.com/mdopp/solarisbay/issues/663)
* **chat:** code-enforce merk-dir durable-fact capture ([b95f298](https://github.com/mdopp/solarisbay/commit/b95f2983594e27dabc93ed3055b475816bcbd62b)), closes [#621](https://github.com/mdopp/solarisbay/issues/621)
* **chat:** code-enforced auto-linking of known entities in replies ([e0d4f23](https://github.com/mdopp/solarisbay/commit/e0d4f23363933b8429e678315c1d9cabb386d8b9)), closes [#694](https://github.com/mdopp/solarisbay/issues/694)
* **chat:** compact device cards on a responsive card grid ([1e659ac](https://github.com/mdopp/solarisbay/commit/1e659ac814f946763633ace3aa1b2dac2c7fc809)), closes [#688](https://github.com/mdopp/solarisbay/issues/688)
* **chat:** context-aware header title on start and household views ([d702982](https://github.com/mdopp/solarisbay/commit/d702982e252a445280d70cc93143e4d13685d291)), closes [#671](https://github.com/mdopp/solarisbay/issues/671)
* **chat:** device-card header toggle + masonry-packed card grid ([dd5c822](https://github.com/mdopp/solarisbay/commit/dd5c822f87f52d20bd10f1beecfda874dcf3fb70)), closes [#692](https://github.com/mdopp/solarisbay/issues/692)
* **chat:** drain the OKF embedding queue into an okf_vectors store ([86d4485](https://github.com/mdopp/solarisbay/commit/86d44851dbf05f72d476eb7958b101ee294af31a)), closes [#650](https://github.com/mdopp/solarisbay/issues/650)
* **chat:** energy history aggregator endpoint for the 24h/7d trend ([b914bb2](https://github.com/mdopp/solarisbay/commit/b914bb25e62c1b68d9748cdaf82ac9d734f333c0)), closes [#689](https://github.com/mdopp/solarisbay/issues/689)
* **chat:** fall back to a single room device when a Cast-group play 500s ([b74c8f8](https://github.com/mdopp/solarisbay/commit/b74c8f804f31cdb49b196800bf16273b83924929))
* **chat:** graphical energy view — Jetzt flow + 24h/7d trend chart ([ed93dae](https://github.com/mdopp/solarisbay/commit/ed93dae7d8ff6f4ae24e9232a49d246e81046d54)), closes [#689](https://github.com/mdopp/solarisbay/issues/689)
* **chat:** graphical energy view + compact device card grid ([c1f9cb3](https://github.com/mdopp/solarisbay/commit/c1f9cb3bdec78ff132c8fe06e9f87fab2696b03b))
* **chat:** imap email ingest adapter ([999b78b](https://github.com/mdopp/solarisbay/commit/999b78b63866a3476171fc83e2fae0886449c136)), closes [#654](https://github.com/mdopp/solarisbay/issues/654)
* **chat:** live status propagation via HA WebSocket + SSE event bus ([62f39f2](https://github.com/mdopp/solarisbay/commit/62f39f2a4735829a49183aa1b092ea1f1f79af0c)), closes [#714](https://github.com/mdopp/solarisbay/issues/714)
* **chat:** merge alias and event hits into notes_search ([7354eb7](https://github.com/mdopp/solarisbay/commit/7354eb72bf75054f9bf0b621285889e214a0e6ef)), closes [#651](https://github.com/mdopp/solarisbay/issues/651)
* **chat:** messenger export drop-folder ingest with whatsapp parser ([6648177](https://github.com/mdopp/solarisbay/commit/6648177ab3008761a2782f1ede94bf98321d4506)), closes [#655](https://github.com/mdopp/solarisbay/issues/655)
* **chat:** mobile bottom tab bar and pwa manifest ([4b9b2cf](https://github.com/mdopp/solarisbay/commit/4b9b2cfc7be50c5059a500fe1a5dcb075a963728)), closes [#648](https://github.com/mdopp/solarisbay/issues/648)
* **chat:** mobile Chats as full-page view bounded above the bottom nav ([d54351e](https://github.com/mdopp/solarisbay/commit/d54351e5146b272bdc8dcbb0241f4181bfc232e5))
* **chat:** mobile Chats as full-page view bounded above the bottom nav ([721b934](https://github.com/mdopp/solarisbay/commit/721b934a9cbefbf6003a482c42635a4e417bc9bb)), closes [#679](https://github.com/mdopp/solarisbay/issues/679)
* **chat:** mobile nav fixes + start-page card picker ([b32afa4](https://github.com/mdopp/solarisbay/commit/b32afa4a27ab3705f8305bc7072ea0c04b38e2be))
* **chat:** nightly bibliothekar curation job ([12ed490](https://github.com/mdopp/solarisbay/commit/12ed490048b17d4c9d001a05be65f74ca27117c2)), closes [#653](https://github.com/mdopp/solarisbay/issues/653)
* **chat:** nightly stenograph extraction over active sessions ([ab0df5d](https://github.com/mdopp/solarisbay/commit/ab0df5dfb5cd44316fc2fe7b8408b81547e3728c)), closes [#652](https://github.com/mdopp/solarisbay/issues/652)
* **chat:** Notizen portal V1 backend — overview, browse, note viewer, search ([6a609a5](https://github.com/mdopp/solarisbay/commit/6a609a5129843bb468fe0f8d8a846e238f087ef3)), closes [#696](https://github.com/mdopp/solarisbay/issues/696)
* **chat:** Notizen portal V1 frontend — page, viewer, 4th nav destination ([165410d](https://github.com/mdopp/solarisbay/commit/165410d6a268f133cb259d000b57df6b5ea5acc4)), closes [#696](https://github.com/mdopp/solarisbay/issues/696)
* **chat:** Notizen portal V2 — inbox curation workbench ([d572b65](https://github.com/mdopp/solarisbay/commit/d572b658bcc3fd11aec6902f3db0b3fec2e3a2cf)), closes [#697](https://github.com/mdopp/solarisbay/issues/697)
* **chat:** Notizen portal V3 — inline editor + statistics ([66f2940](https://github.com/mdopp/solarisbay/commit/66f294059a36a337e1deedb9b064730b4ac4cb08)), closes [#698](https://github.com/mdopp/solarisbay/issues/698) [#699](https://github.com/mdopp/solarisbay/issues/699)
* **chat:** pin_favorite tool + favorites store ([cb3ec9f](https://github.com/mdopp/solarisbay/commit/cb3ec9f40ae88d6e1bcfd9f199c57cd00fb6be12)), closes [#645](https://github.com/mdopp/solarisbay/issues/645)
* **chat:** playlist_add tool writes jellyfin playlists ([0d0260c](https://github.com/mdopp/solarisbay/commit/0d0260c8569f4e8271a62f62616e0e0ac9d1e613)), closes [#647](https://github.com/mdopp/solarisbay/issues/647)
* **chat:** present the energy page much better — metric tiles + circuit list, full-page ([2148bd0](https://github.com/mdopp/solarisbay/commit/2148bd0f1c8c7d6ffe231bbe25fbb1317f5a3c80)), closes [#682](https://github.com/mdopp/solarisbay/issues/682)
* **chat:** progressive thinking indicator — elapsed, model, tool activity ([2d28729](https://github.com/mdopp/solarisbay/commit/2d28729f02e22603f3cef5b7d45c86baac884d98)), closes [#695](https://github.com/mdopp/solarisbay/issues/695)
* **chat:** promote desktop rail Favoriten + Energie to primary nav entries ([fb22127](https://github.com/mdopp/solarisbay/commit/fb22127870bff518aadf53ca4a1e555c87e7f495)), closes [#681](https://github.com/mdopp/solarisbay/issues/681)
* **chat:** promote Favoriten/Energie to primary nav + redesign the energy page ([43ab729](https://github.com/mdopp/solarisbay/commit/43ab7291430c771a11410bd03fb4943c653f4b4c))
* **chat:** propagate a completed turn via SSE or Web Push ([0fe3d47](https://github.com/mdopp/solarisbay/commit/0fe3d47da5eca62d7cb57b5e5a10d1aac5113474)), closes [#715](https://github.com/mdopp/solarisbay/issues/715)
* **chat:** recurring knowledge-night-run cron with re-ingest and embed drain ([8817a12](https://github.com/mdopp/solarisbay/commit/8817a12e5d6b1134161e3287ab2bf82622a4c2ac)), closes [#652](https://github.com/mdopp/solarisbay/issues/652)
* **chat:** round displayed numbers to at most 1 decimal place ([a60d4cf](https://github.com/mdopp/solarisbay/commit/a60d4cfc492099e2e9c847c4ad24762ee71b88d5)), closes [#685](https://github.com/mdopp/solarisbay/issues/685)
* **chat:** semantic retrieval in notes_search ([f872a62](https://github.com/mdopp/solarisbay/commit/f872a621f02188f6b23053b2c7aea9d53ed939e0)), closes [#651](https://github.com/mdopp/solarisbay/issues/651)
* **chat:** Solaris-mobile Phase 1 — Web Push, HA-WS/SSE bus, chat propagation ([46167d5](https://github.com/mdopp/solarisbay/commit/46167d5e4314b027cd2c7626d45bf99cc1e46750))
* **chat:** start page fills the chat viewport and cleaner favorite cards ([16a925b](https://github.com/mdopp/solarisbay/commit/16a925baa14cea30265aae73fbc246d6dccf6b36)), closes [#672](https://github.com/mdopp/solarisbay/issues/672)
* **chat:** start page with favorites api and spa view ([bb6534d](https://github.com/mdopp/solarisbay/commit/bb6534d6a86c5029955e7266fdf416613f490ad8)), closes [#646](https://github.com/mdopp/solarisbay/issues/646)
* **chat:** start-page card picker to browse and add cards on the page ([d7f5476](https://github.com/mdopp/solarisbay/commit/d7f5476d94089e0edc678d6a8a8c59fa81b3732f)), closes [#669](https://github.com/mdopp/solarisbay/issues/669)
* **chat:** Web Push for timers/reminders via VAPID service worker ([33b915d](https://github.com/mdopp/solarisbay/commit/33b915d6ef118871e508bc1fe7a7de220c44da0e)), closes [#713](https://github.com/mdopp/solarisbay/issues/713)
* **ingest:** add signal-cli JSON export parser to the exports registry ([9651ee5](https://github.com/mdopp/solarisbay/commit/9651ee5a68e9ae7fd412b539ed173016a6660aa5)), closes [#661](https://github.com/mdopp/solarisbay/issues/661)
* **ingest:** add SMS/RCS JSON export parser to the exports registry ([ed27428](https://github.com/mdopp/solarisbay/commit/ed27428f695c91054518fbb4bac2cf1fd1189149)), closes [#662](https://github.com/mdopp/solarisbay/issues/662)
* **ingest:** Signal + SMS/RCS chat-export parsers (batch 2026-07-07a) ([e21c6d4](https://github.com/mdopp/solarisbay/commit/e21c6d4bf13d40d0ecd71ba3ca79d7050f05c25f))
* **template:** add VAPID_* env vars for Web Push activation ([b964e41](https://github.com/mdopp/solarisbay/commit/b964e414740707643eb794beebd595e93fa48a03)), closes [#723](https://github.com/mdopp/solarisbay/issues/723)
* Web Push VAPID activation + voice-turn chat propagation (batch 2026-07-11b) ([4f09306](https://github.com/mdopp/solarisbay/commit/4f093066696abe627ebac9fbaeaaafbee03043cd))


### Bug Fixes

* **chat:** bound + prune the notes-portal vault walk so it survives the real Syncthing vault ([7d4a2a1](https://github.com/mdopp/solarisbay/commit/7d4a2a1c454f7261864580c9608626494fe4b0cf)), closes [#705](https://github.com/mdopp/solarisbay/issues/705)
* **chat:** bound the notes-portal vault walk so it survives the real Syncthing vault ([4a68f8e](https://github.com/mdopp/solarisbay/commit/4a68f8e54ed30402ab1fda221334fafde1c31d09))
* **chat:** card picker is selection-only; add sensitive + automations ([fbec090](https://github.com/mdopp/solarisbay/commit/fbec090024b04382a5e72d6decbad3c7624c2611)), closes [#702](https://github.com/mdopp/solarisbay/issues/702)
* **chat:** card-picker safety + Notizen portal V1 ([#702](https://github.com/mdopp/solarisbay/issues/702) [#696](https://github.com/mdopp/solarisbay/issues/696)) ([0b32da7](https://github.com/mdopp/solarisbay/commit/0b32da7c2222a13f79b8f26f1b59b4dc370221d2))
* **chat:** code-enforce canonical journal path + dedup duplicates ([#709](https://github.com/mdopp/solarisbay/issues/709)) ([1b80201](https://github.com/mdopp/solarisbay/commit/1b80201dcd2624f22623b0a9d5e948494bfbefb3))
* **chat:** code-enforce canonical journal path + dedup existing duplicates ([8303de4](https://github.com/mdopp/solarisbay/commit/8303de434cf3a8d4a18531439184aa2e0f804b68)), closes [#709](https://github.com/mdopp/solarisbay/issues/709)
* **chat:** declutter /p/start title — drop in-card heading, header reads "Favoriten" ([03b4ccf](https://github.com/mdopp/solarisbay/commit/03b4ccf36a296bbfe38a2725f4106e3b5c701dfd))
* **chat:** declutter start page — drop in-card "Startseite" heading, header title "Favoriten" ([74e3659](https://github.com/mdopp/solarisbay/commit/74e36593c9fd04d03f17177c2a6b0ed8603753f2)), closes [#677](https://github.com/mdopp/solarisbay/issues/677)
* **chat:** dock the composer above the mobile keyboard ([5488131](https://github.com/mdopp/solarisbay/commit/5488131c601d5c290d29cc2c5ea65ff5bce7dc62)), closes [#673](https://github.com/mdopp/solarisbay/issues/673)
* **chat:** drop the clipped an/aus text on the on/off toggle switch ([90b4c4d](https://github.com/mdopp/solarisbay/commit/90b4c4dbd1581be7631baa6fc8dc7f1abf79e3a0)), closes [#686](https://github.com/mdopp/solarisbay/issues/686)
* **chat:** emit_chat for completed voice turns via the facade ([c821193](https://github.com/mdopp/solarisbay/commit/c821193967ec0a99cc8f70ebea55bd5c2fd5fc6c)), closes [#724](https://github.com/mdopp/solarisbay/issues/724)
* **chat:** energy page uses current-power W sensors, not kWh counters ([e8b5815](https://github.com/mdopp/solarisbay/commit/e8b58159b11c51ca66da553fa730aee1150ac7bb)), closes [#691](https://github.com/mdopp/solarisbay/issues/691)
* **chat:** group-cast fallback — retry a 500 on a single same-area device ([d5809fb](https://github.com/mdopp/solarisbay/commit/d5809fbfdb3a74aba9166a8f998e3ac4ea8edae1)), closes [#638](https://github.com/mdopp/solarisbay/issues/638)
* **chat:** live-refresh the favorites start page ([d264165](https://github.com/mdopp/solarisbay/commit/d264165aacf68e010f81af16e65416a5b1831c01))
* **chat:** live-refresh the favorites start page so entity cards show real state ([921ada5](https://github.com/mdopp/solarisbay/commit/921ada5dafca01ca7cf164db56a62d8cd280a9f2)), closes [#711](https://github.com/mdopp/solarisbay/issues/711)
* **chat:** mobile nav — highlight active tab, drop redundant burger, route Favoriten to /p/start ([a270943](https://github.com/mdopp/solarisbay/commit/a2709436df6155431b57c9dd01d3ea0d3d851e00)), closes [#667](https://github.com/mdopp/solarisbay/issues/667) [#668](https://github.com/mdopp/solarisbay/issues/668)
* **chat:** mobile tab bar active state view-derived + above Chats drawer ([0acb52d](https://github.com/mdopp/solarisbay/commit/0acb52d0ae6b218886c60c308410bfc8a58be7ef))
* **chat:** mobile tab bar active state view-derived + above Chats drawer ([c65382b](https://github.com/mdopp/solarisbay/commit/c65382be6e2df009cbe846b7e359790baef0a099)), closes [#675](https://github.com/mdopp/solarisbay/issues/675)
* **chat:** note_write writes frontmatter-carrying content verbatim ([739faa1](https://github.com/mdopp/solarisbay/commit/739faa151d3d04922388861ac99e61d8570b7d5b)), closes [#657](https://github.com/mdopp/solarisbay/issues/657)
* **chat:** one Zuhause in the desktop rail, highlight derived from the view ([b5c7bd6](https://github.com/mdopp/solarisbay/commit/b5c7bd613ade76b6a9fa66f5b575db37955a1e3e)), closes [#700](https://github.com/mdopp/solarisbay/issues/700)
* **chat:** podcast play retries a group 500 on a same-area device ([2bb74a3](https://github.com/mdopp/solarisbay/commit/2bb74a3af11f452f7cadd4c627f5e32555e0d01f)), closes [#573](https://github.com/mdopp/solarisbay/issues/573)
* **chat:** protect entity_ids from the markdown italic rule ([d7f8d99](https://github.com/mdopp/solarisbay/commit/d7f8d99a95b2a99d7fd885b0c3601c3f0defb12c)), closes [#693](https://github.com/mdopp/solarisbay/issues/693)
* **chat:** tidy energy/HA card number formatting + on/off toggle pill ([a44c127](https://github.com/mdopp/solarisbay/commit/a44c1277316f22af84d1d364347fd24036dae5ab))
* **chat:** voice turns land in the shared zuhause session every resident opens ([0acfa56](https://github.com/mdopp/solarisbay/commit/0acfa560f61a54e20a0000a19908e8aac42ee337)), closes [#649](https://github.com/mdopp/solarisbay/issues/649)

## [0.17.0](https://github.com/mdopp/solarisbay/compare/v0.16.0...v0.17.0) (2026-06-26)


### Features

* **chat:** card media_player with transport + volume controls, same room grid ([defc9ec](https://github.com/mdopp/solarisbay/commit/defc9ece54db9098a21ae96fb7be99ee70d0aaac)), closes [#541](https://github.com/mdopp/solarisbay/issues/541)
* **chat:** card on/off control is a toggle switch, not an 'an'/'aus' button ([88a0dba](https://github.com/mdopp/solarisbay/commit/88a0dba6dc3214a2ba46410a86db02212ab30feb)), closes [#560](https://github.com/mdopp/solarisbay/issues/560)
* **chat:** card on/off toggle + media_player power/source + failed-voice-turn traces ([2df9601](https://github.com/mdopp/solarisbay/commit/2df9601bcb1c4af54db387565f6b25ff4154bb71))
* **chat:** card-layout v2 — compact-all + full-width answers + uniform base-column grid ([#552](https://github.com/mdopp/solarisbay/issues/552)) ([1b48c2b](https://github.com/mdopp/solarisbay/commit/1b48c2b3ad94b8079c67b9723480e0f269b93fd8))
* **chat:** chat-cards epic [#534](https://github.com/mdopp/solarisbay/issues/534) — state-filtering, room grouping, compact card, grid, room query, media_player ([f4c4ae5](https://github.com/mdopp/solarisbay/commit/f4c4ae5245508cc07f5253239f0a088398913129))
* **chat:** compact light card — on/off badge + brightness slider one row ([dcdd6eb](https://github.com/mdopp/solarisbay/commit/dcdd6eba85fe1cc4e629589f04c94eca8933cf75)), closes [#538](https://github.com/mdopp/solarisbay/issues/538)
* **chat:** concrete read-only HttpDavClient for CalDAV/CardDAV ingest ([aa66ae0](https://github.com/mdopp/solarisbay/commit/aa66ae0738b06025ca3a03b7ace7f51ce5ed88ed))
* **chat:** concrete read-only HttpDavClient for CalDAV/CardDAV ingest ([d4e5bb8](https://github.com/mdopp/solarisbay/commit/d4e5bb8ae22ff53603238cb1185527647234fa81)), closes [#522](https://github.com/mdopp/solarisbay/issues/522)
* **chat:** continuous conversation — a follow-up turn's spoken text ends in a question mark ([e227c7c](https://github.com/mdopp/solarisbay/commit/e227c7c80428b1a6ca1a47f63196b351161c673f)), closes [#566](https://github.com/mdopp/solarisbay/issues/566)
* **chat:** continuous conversation — follow-up turns end in '?' so HA re-opens the mic without re-wake ([1e23cfe](https://github.com/mdopp/solarisbay/commit/1e23cfee2cab98a188e1f8a9b6623593fc3efc17))
* **chat:** default device-less play to the current room (u99) ([44baab0](https://github.com/mdopp/solarisbay/commit/44baab0334e413d7bf7941d5f9f7975461496915))
* **chat:** default music/radio to the current room's device ([2f7a042](https://github.com/mdopp/solarisbay/commit/2f7a042595634b6b826003ad35173fd28915e2cd))
* **chat:** deterministic engine-side confirmation gate for sensitive ha_call_service ([d4aebd8](https://github.com/mdopp/solarisbay/commit/d4aebd8c5c07898bec98f98d6a7d024afbd13401))
* **chat:** deterministic engine-side confirmation gate for sensitive ha_call_service ([b77858e](https://github.com/mdopp/solarisbay/commit/b77858e9ec172ab22b20c60a4d35adbf193fa330)), closes [#570](https://github.com/mdopp/solarisbay/issues/570)
* **chat:** enrich bands with genre+bio facts + artist_info query op ([57f59ca](https://github.com/mdopp/solarisbay/commit/57f59ca1a6ac92ac43894fea3b054bed815c5e2d))
* **chat:** enrich bands with genre+bio facts + artist_info query op ([a457ffc](https://github.com/mdopp/solarisbay/commit/a457ffc2b57d7452a8805288d0996f0c4493b07b)), closes [#592](https://github.com/mdopp/solarisbay/issues/592)
* **chat:** group cards by room when &gt;4, else label each card ([eb2531d](https://github.com/mdopp/solarisbay/commit/eb2531d77dbba0a30b725285900b01e5752c9a93)), closes [#537](https://github.com/mdopp/solarisbay/issues/537)
* **chat:** Jellyfin music ingest -&gt; central knowledge OKF concepts ([316143f](https://github.com/mdopp/solarisbay/commit/316143ffe193baeb1e5efce4c80a1fd5aac69369))
* **chat:** Jellyfin music ingest -&gt; central knowledge OKF concepts ([e134bb5](https://github.com/mdopp/solarisbay/commit/e134bb5da1af143e3be2a224c957694583873ec2)), closes [#564](https://github.com/mdopp/solarisbay/issues/564)
* **chat:** learned default playback device — ask once, store, reuse ([c82f7c1](https://github.com/mdopp/solarisbay/commit/c82f7c17fe315dfb88028bed23fd365c8682b6b4)), closes [#622](https://github.com/mdopp/solarisbay/issues/622)
* **chat:** learned default playback device (ask once, store, reuse) ([e890b3c](https://github.com/mdopp/solarisbay/commit/e890b3c05e193b5f74318910144d23b788c124d0))
* **chat:** media_player card power on/off toggle + source select ([492cf9e](https://github.com/mdopp/solarisbay/commit/492cf9e546e148c31742a816a77d0a823d10f240)), closes [#561](https://github.com/mdopp/solarisbay/issues/561)
* **chat:** music_query structured artist-&gt;songs tool (per-user scoped) ([b9adab9](https://github.com/mdopp/solarisbay/commit/b9adab96e10e8fc764cf33628ff1a1e0a1551853))
* **chat:** music_query tool over the structured store ([46160b8](https://github.com/mdopp/solarisbay/commit/46160b87031c2bce143f52c1fc3b151e34655776)), closes [#588](https://github.com/mdopp/solarisbay/issues/588)
* **chat:** notes_search fuzzy + ranked retrieval (non-embedding slice of [#591](https://github.com/mdopp/solarisbay/issues/591)) ([ba073c7](https://github.com/mdopp/solarisbay/commit/ba073c728c63ece7b771248517259e2ab4b1331f))
* **chat:** notes_search lightly-fuzzy + ranked, shared scorer ([37ce410](https://github.com/mdopp/solarisbay/commit/37ce4108cc3aef96af80f99e16f4212be0fed825)), closes [#591](https://github.com/mdopp/solarisbay/issues/591)
* **chat:** on-demand song lyrics — resolve song, fetch live from Jellyfin ([3dcca18](https://github.com/mdopp/solarisbay/commit/3dcca185a0d0159c04673073b573870f27b845f6)), closes [#593](https://github.com/mdopp/solarisbay/issues/593)
* **chat:** on-demand song lyrics — resolve song, fetch live from Jellyfin ([#593](https://github.com/mdopp/solarisbay/issues/593)) ([fd9db46](https://github.com/mdopp/solarisbay/commit/fd9db46e9bfa5dcdab0b5b49f3ad24b986ec9e34))
* **chat:** path-based ownership — users/&lt;uid&gt;/ private paths + OKF resident: leak fix + per-library music ([72875f9](https://github.com/mdopp/solarisbay/commit/72875f9bae26406bfb753ab0124d3d684b009263))
* **chat:** path-based ownership — users/&lt;uid&gt;/ private paths + OKF resident: leak fix + per-library music ([1dc3b32](https://github.com/mdopp/solarisbay/commit/1dc3b32a1104d2312fb88577b9fdda220dce7ade)), closes [#576](https://github.com/mdopp/solarisbay/issues/576)
* **chat:** per-user privacy slice 1 — uid-filtered retrieval, default-deny ([02a5bfa](https://github.com/mdopp/solarisbay/commit/02a5bfab3b1bb544c4c2772e1f5ee53fff3359f7))
* **chat:** per-user privacy slice 1 — uid-filtered retrieval, default-deny ([776bbb6](https://github.com/mdopp/solarisbay/commit/776bbb601734cd87a3eaa1281d36df396b0289ad)), closes [#576](https://github.com/mdopp/solarisbay/issues/576)
* **chat:** persist a failed voice turn's trace so the error is visible ([d85f504](https://github.com/mdopp/solarisbay/commit/d85f5045922ea009aeee50d8357701f2db036d63)), closes [#562](https://github.com/mdopp/solarisbay/issues/562)
* **chat:** play_music — cast a Jellyfin library track on a media_player ([cc15c4c](https://github.com/mdopp/solarisbay/commit/cc15c4cb6e81d24dfd569a3c161f8104bf2919d6))
* **chat:** play_music — cast a Jellyfin library track on a media_player ([9b6ccc5](https://github.com/mdopp/solarisbay/commit/9b6ccc5c2e3e4dc703385d494a3da6d29406cdcd)), closes [#604](https://github.com/mdopp/solarisbay/issues/604)
* **chat:** play_radio — favorite station, ask+store, via radio-browser ([df5e93a](https://github.com/mdopp/solarisbay/commit/df5e93a36938b172fddf1edfa51096f3b705df6e))
* **chat:** play_radio — per-user favorite station via radio-browser ([07eca33](https://github.com/mdopp/solarisbay/commit/07eca33f9eb263b5f068330f727876ae81c853a9))
* **chat:** podcast search by host/person via fyyd term fallback ([40b713a](https://github.com/mdopp/solarisbay/commit/40b713aebe7310589bce20ce1c077c89cc1cdd64))
* **chat:** quick-reply answers on every question + composer pre-fill ([66fc771](https://github.com/mdopp/solarisbay/commit/66fc7711078b2bc24ff4849fb75a3b45802e204a))
* **chat:** quick-reply chips — model offers 2-4 clickable answers above the input ([6b3e9db](https://github.com/mdopp/solarisbay/commit/6b3e9db1d1c7358b5fc0dad7eababe9318a7cd2f)), closes [#555](https://github.com/mdopp/solarisbay/issues/555)
* **chat:** quick-reply suggestion chips — model offers 2-4 clickable answers ([df02fc1](https://github.com/mdopp/solarisbay/commit/df02fc1e5c81cb85c3ac10849e8e8aadde22ce21))
* **chat:** render cards only for entities matching the query state ([3e294de](https://github.com/mdopp/solarisbay/commit/3e294de3d14f2423bc1c7e3f9bbb12cd5266c286)), closes [#536](https://github.com/mdopp/solarisbay/issues/536)
* **chat:** research(query) tool — fan out to notes+web, trust-rank, cited summary ([5572132](https://github.com/mdopp/solarisbay/commit/557213234a89c9d2dc485765d6734fe884d3e3db)), closes [#574](https://github.com/mdopp/solarisbay/issues/574)
* **chat:** research(query) tool — fan out to notes+web, trust-rank, cited summary (slice 1) ([2e5f256](https://github.com/mdopp/solarisbay/commit/2e5f256b63104334d9015aebfb149659ac16f9fb))
* **chat:** resolve podcasts by host/person via fyyd term search ([631a9da](https://github.com/mdopp/solarisbay/commit/631a9dacf231a6193dbdf4b7e46f2b910bb45a5a)), closes [#568](https://github.com/mdopp/solarisbay/issues/568)
* **chat:** responsive side-by-side card grid — invisible auto-fill columns ([123ba00](https://github.com/mdopp/solarisbay/commit/123ba007fe4c3f33d6f8534cb404d658453ed2eb)), closes [#539](https://github.com/mdopp/solarisbay/issues/539)
* **chat:** room query cards all the room's actuators under one header ([18fa71e](https://github.com/mdopp/solarisbay/commit/18fa71edd7acc20eacc7aa258f26e00bed3e9b76)), closes [#540](https://github.com/mdopp/solarisbay/issues/540)
* **chat:** rooms first-class — list rooms, resolve device→room, friendly names ([04f67a0](https://github.com/mdopp/solarisbay/commit/04f67a043bc8fe233aa94e1933b0387725fe0c01)), closes [#535](https://github.com/mdopp/solarisbay/issues/535)
* **chat:** rooms first-class — list rooms, resolve device→room, never leak entity_ids ([08ab8a5](https://github.com/mdopp/solarisbay/commit/08ab8a5eac8857c15dac86bfac6b82af44cb0901))
* **chat:** suggest quick-reply answers for every question + pre-fill favorite ([4797e6b](https://github.com/mdopp/solarisbay/commit/4797e6baf7738b4c4668ae50d80b12f8e634eff0))
* **chat:** trigger OKF ingest adapters on engine boot ([dafdc67](https://github.com/mdopp/solarisbay/commit/dafdc67af1517e1cb2485c336b896e091856e9f2))
* **chat:** trigger OKF ingest adapters on engine boot ([49964f4](https://github.com/mdopp/solarisbay/commit/49964f46d5ef852299b1d653633c088758c8b0ae)), closes [#517](https://github.com/mdopp/solarisbay/issues/517)
* **skill:** SOUL — add second-brain section + tighten/dedupe ([8c2100f](https://github.com/mdopp/solarisbay/commit/8c2100f31aadc29622db33e65c0815c781e3f3a7))
* **skill:** SOUL — add second-brain section + tighten/dedupe ([#615](https://github.com/mdopp/solarisbay/issues/615) [#617](https://github.com/mdopp/solarisbay/issues/617)) ([ff5c87e](https://github.com/mdopp/solarisbay/commit/ff5c87eb11798f639f152ae6ace4792be497c514))
* **solaris:** add user-guide.md for the ServiceBay portal ([#533](https://github.com/mdopp/solarisbay/issues/533)) ([317981e](https://github.com/mdopp/solarisbay/commit/317981edcdbc5cfc0512b065dedf4260a5307ca2))
* **template:** add streaming microWakeWord Solaris model ([#542](https://github.com/mdopp/solarisbay/issues/542)) ([56a3862](https://github.com/mdopp/solarisbay/commit/56a386244f0e0c811fe284e0d2df0a7047f2bd55))
* **template:** derive JELLYFIN_CAST_URL from the box LAN IP ([e880c2b](https://github.com/mdopp/solarisbay/commit/e880c2be448eb3a1e04e16f3eb9871dcfe51c50e))
* **template:** derive JELLYFIN_CAST_URL from the box LAN IP ([960a03d](https://github.com/mdopp/solarisbay/commit/960a03dabf7d46f4250c479fa34f3020b04eae08)), closes [#607](https://github.com/mdopp/solarisbay/issues/607)


### Bug Fixes

* **chat:** boot ingest waits for source health / bounded retry — no 0-ingest boot ([5f10fa6](https://github.com/mdopp/solarisbay/commit/5f10fa611e7c71baf213df77b05ddb979114d49b))
* **chat:** card grid — 1 full-width column on narrow/phone, no control overflow ([bab1618](https://github.com/mdopp/solarisbay/commit/bab1618f33d6b422487a281422fa394716460b01))
* **chat:** card grid 1-col full-width on narrow, no control overflow ([3694845](https://github.com/mdopp/solarisbay/commit/369484559f9d9a872845b1118303fba1f205c6f8)), closes [#553](https://github.com/mdopp/solarisbay/issues/553)
* **chat:** cast music via a LAN-reachable Jellyfin URL not localhost ([9684407](https://github.com/mdopp/solarisbay/commit/9684407b7aed605aa98da2800a273c1a630a6685)), closes [#604](https://github.com/mdopp/solarisbay/issues/604)
* **chat:** close fail-open holes in the confirmation gate ([48b1f45](https://github.com/mdopp/solarisbay/commit/48b1f45641c1ce5454679eddefe9f380a3d6565d))
* **chat:** compact_history keeps tool_calls args (device-control regression) ([#637](https://github.com/mdopp/solarisbay/issues/637)) ([93af311](https://github.com/mdopp/solarisbay/commit/93af3113e3080532bcd7586cf946dbf2ce891582))
* **chat:** confirm-worthy actions ask via offer_choices and wait ([2b1aadb](https://github.com/mdopp/solarisbay/commit/2b1aadb455c6235d15c4f8d6946ce4688d66e4de))
* **chat:** confirm-worthy actions ask via offer_choices and wait ([520fe3e](https://github.com/mdopp/solarisbay/commit/520fe3e71b100ab1494f81e5ac4b03237a8b37f7)), closes [#558](https://github.com/mdopp/solarisbay/issues/558)
* **chat:** continue_conversation on a pending question, not only a trailing '?' ([55685f7](https://github.com/mdopp/solarisbay/commit/55685f7fed290bad7a48eaf0e4d8f77b38295674)), closes [#627](https://github.com/mdopp/solarisbay/issues/627)
* **chat:** degrade Jellyfin lyrics fetch errors to None, not a broken turn ([b0d79ea](https://github.com/mdopp/solarisbay/commit/b0d79eaf78ce5c932118a7c1544fdd32d9542a9a)), closes [#593](https://github.com/mdopp/solarisbay/issues/593)
* **chat:** drop 'bitte' from confirm-gate affirmatives — residual fail-open ([7d09928](https://github.com/mdopp/solarisbay/commit/7d099280c769e390b268fe911ba21bf5a600414d))
* **chat:** escape LIKE wildcards in music_query artist/prefix arg ([f4a9af8](https://github.com/mdopp/solarisbay/commit/f4a9af84a0edd528dc829ea37d392a45fb3b40dc))
* **chat:** eval edge-case fixes — card room labels, area-cache, room-match, VALARM nesting ([26ebcfc](https://github.com/mdopp/solarisbay/commit/26ebcfcd0a859fb5c032dd39dc5b5c8c13360a70))
* **chat:** eval edge-case fixes — card room labels, area-cache, room-match, VALARM nesting ([f476819](https://github.com/mdopp/solarisbay/commit/f476819f776c62064673fe3bf71f714e2a1b0876)), closes [#545](https://github.com/mdopp/solarisbay/issues/545) [#546](https://github.com/mdopp/solarisbay/issues/546) [#547](https://github.com/mdopp/solarisbay/issues/547) [#548](https://github.com/mdopp/solarisbay/issues/548)
* **chat:** immich ingest bounded page-retry, cursor checkpoint, O(1) embed enqueue ([2665b7b](https://github.com/mdopp/solarisbay/commit/2665b7b29e5907104b79b1e5b37201f23c44af4e)), closes [#597](https://github.com/mdopp/solarisbay/issues/597)
* **chat:** immich ingest robustness — page-retry, cursor checkpoint, O(1) embed enqueue ([9f846be](https://github.com/mdopp/solarisbay/commit/9f846bee729b464db84b8474e4fd278f6a6b8aae))
* **chat:** ingest-adapter robustness — per-item isolation, persisted sync cursors, VALARM parse ([a91ce2c](https://github.com/mdopp/solarisbay/commit/a91ce2cb28617fb566ebd7dbaf1cc5d3a79d4465))
* **chat:** ingest-adapter robustness — per-item isolation, sync cursors, VALARM parse ([a387d10](https://github.com/mdopp/solarisbay/commit/a387d10c9b4d041077394ed8c8337cda156977ca)), closes [#528](https://github.com/mdopp/solarisbay/issues/528) [#529](https://github.com/mdopp/solarisbay/issues/529) [#527](https://github.com/mdopp/solarisbay/issues/527)
* **chat:** Jellyfin ingest re-auth on 401 + empty-slug id fallback ([8c622b0](https://github.com/mdopp/solarisbay/commit/8c622b02d455206b20297489a75a640b2baa11ab))
* **chat:** Jellyfin ingest re-auth on 401 + empty-slug id fallback ([2944a7d](https://github.com/mdopp/solarisbay/commit/2944a7d4f6431b52f7a0a231555a60754469461d)), closes [#583](https://github.com/mdopp/solarisbay/issues/583)
* **chat:** Jellyfin libraries() uses user-scoped /Users/{id}/Views ([0a1823a](https://github.com/mdopp/solarisbay/commit/0a1823a53e0c2593875ea7745afc429d6d07b6ec))
* **chat:** Jellyfin libraries() uses user-scoped /Users/{id}/Views ([997bf29](https://github.com/mdopp/solarisbay/commit/997bf29d4566cddc26f102b0758c8c0086e4b149)), closes [#581](https://github.com/mdopp/solarisbay/issues/581)
* **chat:** Jellyfin re-auth fires on a raised 401 mid-pagination ([614592d](https://github.com/mdopp/solarisbay/commit/614592dfcd4be0967f967d1e2cd56fc1c5779a2f))
* **chat:** Jellyfin re-auth fires on a raised 401 mid-pagination ([5cd9201](https://github.com/mdopp/solarisbay/commit/5cd9201779130fb76c77205962a17d44ad1e5ae7)), closes [#583](https://github.com/mdopp/solarisbay/issues/583)
* **chat:** keep chat responsive during OKF ingest — WAL + off-loop ingest ([#587](https://github.com/mdopp/solarisbay/issues/587)) ([80d772d](https://github.com/mdopp/solarisbay/commit/80d772d2c2dd82412f407083106466e18bb6aa2f))
* **chat:** keep the mic open when a question is pending (continuous conversation) ([91bfbd2](https://github.com/mdopp/solarisbay/commit/91bfbd29d42a6f256b9059932519730953a8206d))
* **chat:** music_query ranked fuzzy artist resolve — exact wins first ([ce43e0f](https://github.com/mdopp/solarisbay/commit/ce43e0fe2984253118c5b6415901e6d45a03dd45)), closes [#590](https://github.com/mdopp/solarisbay/issues/590)
* **chat:** music_query ranked fuzzy artist resolve — exact wins, else fuzzy ([f5f7adc](https://github.com/mdopp/solarisbay/commit/f5f7adced3c9535c640655f43694f0c0da03aff7))
* **chat:** Obsidian ingest robustness — per-note isolation + graceful unknown-type skip ([166229d](https://github.com/mdopp/solarisbay/commit/166229d79fa21ec24fe8ebb3ae1d5125d02b6788))
* **chat:** per-note isolation and graceful unknown-type skip in Obsidian ingest ([2a50328](https://github.com/mdopp/solarisbay/commit/2a503280973dadd5fb1524c4e7384cdace9e7fb0)), closes [#520](https://github.com/mdopp/solarisbay/issues/520)
* **chat:** play_music casts direct/static Jellyfin stream to fix Cast-GROUP HA 500 ([fdad83f](https://github.com/mdopp/solarisbay/commit/fdad83f29381acb347c1daf0b811c65111afef21))
* **chat:** play_music casts direct/static Jellyfin URL first, /universal fallback ([107bc17](https://github.com/mdopp/solarisbay/commit/107bc172c6858ae8d157d107bb4022ef6674f07e)), closes [#604](https://github.com/mdopp/solarisbay/issues/604)
* **chat:** play_music casts from a LAN-reachable Jellyfin URL ([f0e6c6e](https://github.com/mdopp/solarisbay/commit/f0e6c6eb35d196b58096ec17574287a9090ef835))
* **chat:** play_music v2 — random play, 'Song von X' parse fix, surface+retry HA errors ([56161f6](https://github.com/mdopp/solarisbay/commit/56161f60cdc4eab7cef6c5a11b29afea0fa37398))
* **chat:** play_music v2 — random play, 'Song von X' parse fix, surface+retry HA errors ([c2b8f55](https://github.com/mdopp/solarisbay/commit/c2b8f5592d4ced83a90c21e885000c3ee30d5f99)), closes [#604](https://github.com/mdopp/solarisbay/issues/604)
* **chat:** podcast search — keep the verbatim name + fuzzy best-match against fyyd ([0f39a32](https://github.com/mdopp/solarisbay/commit/0f39a32bff784ff9dfc033a2b45c13c389da84ec)), closes [#563](https://github.com/mdopp/solarisbay/issues/563)
* **chat:** podcast search — keep verbatim name + fuzzy best-match against fyyd ([29fd892](https://github.com/mdopp/solarisbay/commit/29fd892d1150ee5819ee4c746121daa0bdea3c09))
* **chat:** radio — sanitize favorite name/url before frontmatter write ([17dc249](https://github.com/mdopp/solarisbay/commit/17dc2498710f9d476fa1834365b72119ee020a9c))
* **chat:** remove context-window fill ring from send button ([1fcac6a](https://github.com/mdopp/solarisbay/commit/1fcac6a5f255f14038c992a46eff8bd3e263d48c))
* **chat:** scope private-note ingest + contain note_write to caller subtree ([b3f08e5](https://github.com/mdopp/solarisbay/commit/b3f08e531cca5f4c81894d0b088537f0a0dc9c46))
* **chat:** solaris.db WAL + busy_timeout on all connect paths ([78b3582](https://github.com/mdopp/solarisbay/commit/78b358239994384aabbdef987ff2dbf2c44c4cec))
* **chat:** solaris.db WAL + busy_timeout on all connect paths ([181e0f2](https://github.com/mdopp/solarisbay/commit/181e0f275761b9428ff4a05fc890304ad422b051)), closes [#600](https://github.com/mdopp/solarisbay/issues/600)
* **chat:** strip [[ ]] cross-links to plain text on the voice/facade path ([c714242](https://github.com/mdopp/solarisbay/commit/c714242e8e559e5c10d270c4a975e33251c8a178))
* **chat:** strip [[ ]] cross-links to plain text on the voice/facade path ([2b1639e](https://github.com/mdopp/solarisbay/commit/2b1639ec6a8d95ce969d4bca76bb4d04b2b9aacf)), closes [#616](https://github.com/mdopp/solarisbay/issues/616)
* **chat:** wait for source health before boot ingest, no 0-ingest boot ([6c35777](https://github.com/mdopp/solarisbay/commit/6c357773975100a48db6a44f8afaf7c3b273151d)), closes [#531](https://github.com/mdopp/solarisbay/issues/531)
* **template:** solaris post-deploy converges JELLYFIN_PASSWORD self-heal ([a4ab91c](https://github.com/mdopp/solarisbay/commit/a4ab91cad66a91337c3d3f10629d3b12c8c9ec81)), closes [#626](https://github.com/mdopp/solarisbay/issues/626)
* **template:** solaris post-deploy self-heals the Jellyfin service credential ([6b5703d](https://github.com/mdopp/solarisbay/commit/6b5703db2f99858f1b630ed604f7aefb6ca02025))
* **voice:** self-heal whisper STT — health probe + kill-on-failure ([#611](https://github.com/mdopp/solarisbay/issues/611)) ([dea2d7e](https://github.com/mdopp/solarisbay/commit/dea2d7e1977d369fb6f8a0d764c5c4e4c34b56b2))
* **voice:** THOROUGH_MODEL → gemma4:e4b for VRAM headroom ([#612](https://github.com/mdopp/solarisbay/issues/612)) ([a8411d5](https://github.com/mdopp/solarisbay/commit/a8411d55a928d69b002a0d59f10fd5a6cd52136a))


### Performance Improvements

* **chat:** compact past-turn tool calls in the model context ([721ba4f](https://github.com/mdopp/solarisbay/commit/721ba4fe525b651349aac91330336b6b41344d8d))
* **chat:** compact prior turns' tool args+results in the model context ([7db7c69](https://github.com/mdopp/solarisbay/commit/7db7c694cca7fcc3dfc9e897817fe5bb86c4cab4)), closes [#623](https://github.com/mdopp/solarisbay/issues/623)
* **chat:** dispatch a turn's tool calls concurrently (bounded 5), gate-first ([8623b6f](https://github.com/mdopp/solarisbay/commit/8623b6fb150a59227fb9071ace20bd2b6826997a))
* **chat:** dispatch a turn's tool calls concurrently, gate-first ([4bb0a4f](https://github.com/mdopp/solarisbay/commit/4bb0a4f08df33b6826f9a64a8a3288d05d5d1f90)), closes [#624](https://github.com/mdopp/solarisbay/issues/624)

## [0.16.0](https://github.com/mdopp/solarisbay/compare/v0.15.0...v0.16.0) (2026-06-21)


### Features

* **chat:** anchor→OKF resolution, [[ ]] cross-links, and household energy page ([#509](https://github.com/mdopp/solarisbay/issues/509)) ([508b965](https://github.com/mdopp/solarisbay/commit/508b965201aad7ecf19fdf0372866da0137042c6))
* **chat:** scoped HA cards, concept/entity page, follow-up chips, auto-anchors ([#507](https://github.com/mdopp/solarisbay/issues/507)) ([392e6a6](https://github.com/mdopp/solarisbay/commit/392e6a636679bde4682c88292804ba7860e11399))
* **chat:** span concept-page backlinks across chat and vault [[ ]] links ([8b76165](https://github.com/mdopp/solarisbay/commit/8b76165be83794add7d3b57ed837f09440176a12)), closes [#505](https://github.com/mdopp/solarisbay/issues/505)
* **skill:** find and play podcasts via the keyless fyyd.de index ([72d75e5](https://github.com/mdopp/solarisbay/commit/72d75e5e0e47b468c97638d8590ea3a792c44382))
* **skill:** find and play podcasts via the keyless fyyd.de index ([bc24f70](https://github.com/mdopp/solarisbay/commit/bc24f70f8f9d60ca43ceec282203e2fe7c7a36f0)), closes [#513](https://github.com/mdopp/solarisbay/issues/513)
* **skill:** Jellyfin media control + internet radio media skill, play_media legend ([7db7519](https://github.com/mdopp/solarisbay/commit/7db7519c2ce3c3f67d19fa6b4bb2e1400f864bdc)), closes [#511](https://github.com/mdopp/solarisbay/issues/511) [#512](https://github.com/mdopp/solarisbay/issues/512)
* Solaris wakeword convergence + media control skill ([def92ba](https://github.com/mdopp/solarisbay/commit/def92ba448b32d72a3c12ba31c6527d34f017334))


### Bug Fixes

* **template:** converge wake_word_id so Solaris wakeword stays active on redeploy ([52641b5](https://github.com/mdopp/solarisbay/commit/52641b51ea2003e1c93dbd9fdab325e2c6577d03)), closes [#514](https://github.com/mdopp/solarisbay/issues/514)

## [0.15.0](https://github.com/mdopp/solarisbay/compare/v0.14.0...v0.15.0) (2026-06-17)


### Features

* **chat:** chat UI polish, definition kind taxonomy phase 1, and read-only HA cards phase 1 ([#493](https://github.com/mdopp/solarisbay/issues/493)) ([c41616e](https://github.com/mdopp/solarisbay/commit/c41616eef1d58be524117594618c1539c1ef4e1f))
* **chat:** HA group cards for multiple entities ([#497](https://github.com/mdopp/solarisbay/issues/497)) ([bc76685](https://github.com/mdopp/solarisbay/commit/bc766858d0ce006fce0aaf0d9ec011cf38562e91)), closes [#478](https://github.com/mdopp/solarisbay/issues/478)
* **chat:** OKF concept write-path core for ingest adapters ([57aadc8](https://github.com/mdopp/solarisbay/commit/57aadc855a094c705388380ec736034f6c56bda5)), closes [#447](https://github.com/mdopp/solarisbay/issues/447)
* **chat:** scheduler and hooks editors with pickers, HA card sliders, colour, and climate ([#496](https://github.com/mdopp/solarisbay/issues/496)) ([c8b7e0b](https://github.com/mdopp/solarisbay/commit/c8b7e0bde0249a0198dd130e1c43ae8d4ddeb457))
* **chat:** separate Voice card from Model + let setting cards size to content ([#471](https://github.com/mdopp/solarisbay/issues/471)) ([481262c](https://github.com/mdopp/solarisbay/commit/481262c102da7fc0360b0d455e8cc5ef346cfd3d))
* **chat:** skill taxonomy reorg + /skills and /commands editors, HA toggle cards, CI path-filter ([#495](https://github.com/mdopp/solarisbay/issues/495)) ([3240846](https://github.com/mdopp/solarisbay/commit/3240846a177403a640ce5e2c8ec4945692bb2b11))
* **db:** add OKF knowledge-index tables ([75c8280](https://github.com/mdopp/solarisbay/commit/75c828091a28072089bd557fa77cf084ceb402c3)), closes [#446](https://github.com/mdopp/solarisbay/issues/446)
* **engine:** bound the durable household chat by head-truncation ([#466](https://github.com/mdopp/solarisbay/issues/466)) ([7b9a24e](https://github.com/mdopp/solarisbay/commit/7b9a24e117a35061994ea565ab54bae7866d2152))
* **engine:** discover read-only devices by query instead of packing the prompt ([#463](https://github.com/mdopp/solarisbay/issues/463)) ([128cd28](https://github.com/mdopp/solarisbay/commit/128cd2807e8c28148661c0ca603481b1cbe0636b))
* **ingest:** calendar + contacts adapter — CalDAV/CardDAV to OKF concepts ([1779e69](https://github.com/mdopp/solarisbay/commit/1779e69462413a699c92454c874480ef8c291e98)), closes [#207](https://github.com/mdopp/solarisbay/issues/207)
* **ingest:** Immich photo adapter — assets/faces/EXIF-geo to OKF concepts ([30ee597](https://github.com/mdopp/solarisbay/commit/30ee597d98da52a514fdabe438e41b27b07df806)), closes [#206](https://github.com/mdopp/solarisbay/issues/206)
* **ingest:** Obsidian adapter — existing vault notes to OKF concepts ([e80ccc6](https://github.com/mdopp/solarisbay/commit/e80ccc6ed11bdba928e6b86d812363fdfcfe171e)), closes [#448](https://github.com/mdopp/solarisbay/issues/448)
* Phase-1 household-knowledge ingestion — OKF write-path + adapters ([8f46a44](https://github.com/mdopp/solarisbay/commit/8f46a44516bc2129f9561e24f9d0017e541a3efe))
* **skill:** terser household replies + time-only clock answers ([#464](https://github.com/mdopp/solarisbay/issues/464)) ([8ebc8aa](https://github.com/mdopp/solarisbay/commit/8ebc8aabf9838002c48254422d1548dd9400fcc3))
* **template:** add trained Solaris wake-word model ([#459](https://github.com/mdopp/solarisbay/issues/459)) ([65893c5](https://github.com/mdopp/solarisbay/commit/65893c54839d2cfcd54a4774f1c88993406e000d))
* **template:** own the full voice pipeline in the solaris stack ([08b6030](https://github.com/mdopp/solarisbay/commit/08b603003a8e7fc6daa8831c64a50297864d14ba))
* **template:** own the voice pipeline in the solaris stack ([8d94695](https://github.com/mdopp/solarisbay/commit/8d9469511ab333646fc835301d6aa520f6994e73)), closes [#456](https://github.com/mdopp/solarisbay/issues/456)
* **template:** Solaris owns the openWakeWord wake engine ([#460](https://github.com/mdopp/solarisbay/issues/460)) ([7c50010](https://github.com/mdopp/solarisbay/commit/7c500108a267499a7a84bdfb607780cfad3e7773))


### Bug Fixes

* **chat:** attach per-turn step traces to the correct turn ([#465](https://github.com/mdopp/solarisbay/issues/465)) ([8f3252f](https://github.com/mdopp/solarisbay/commit/8f3252f6e5a7f0aae5566ec3c9dc27fa47e3c3fd))
* **chat:** clear admin-only handling instead of a thin "admins" line ([#470](https://github.com/mdopp/solarisbay/issues/470)) ([233e12c](https://github.com/mdopp/solarisbay/commit/233e12c3e9f0d68275fee8f7ddcd9b2e43f0256c))
* **chat:** collapse the per-turn step trace by default ([#492](https://github.com/mdopp/solarisbay/issues/492)) ([645f684](https://github.com/mdopp/solarisbay/commit/645f6842619cc929a8e5287c660dac041e750a93))
* **chat:** give setting cards a min body height so they aren't a thin line ([#468](https://github.com/mdopp/solarisbay/issues/468)) ([6d17c84](https://github.com/mdopp/solarisbay/commit/6d17c8474c1057c80b7b19a499d3df65d5a51d8b))
* **chat:** keep long mobile streams alive and resume on a transport drop ([f86bd03](https://github.com/mdopp/solarisbay/commit/f86bd0339df70f3e6e265a71e8b6abeac133f47a)), closes [#452](https://github.com/mdopp/solarisbay/issues/452)
* **chat:** match skill slash-commands case-insensitively ([#469](https://github.com/mdopp/solarisbay/issues/469)) ([8e030f9](https://github.com/mdopp/solarisbay/commit/8e030f919c59175d67630557397c0b02ccd3fa35))
* **chat:** persist per-turn step-trace detail body so it survives reload + restart ([7a61e1d](https://github.com/mdopp/solarisbay/commit/7a61e1ded93f129ff77338d5f66566a72dbb46ff)), closes [#451](https://github.com/mdopp/solarisbay/issues/451)
* **chat:** persist step-trace detail + keep long mobile streams alive ([4642945](https://github.com/mdopp/solarisbay/commit/4642945935dd0b7da786e8e11435f03376fc56f8))
* **chat:** prune session_traces on delete and head-truncation ([#490](https://github.com/mdopp/solarisbay/issues/490)) ([c4cc22d](https://github.com/mdopp/solarisbay/commit/c4cc22dbd5bb75d7e3942c4972786a3c7f28ba46))
* **chat:** return message created_at so persisted traces render on reopen ([#491](https://github.com/mdopp/solarisbay/issues/491)) ([f9c3392](https://github.com/mdopp/solarisbay/commit/f9c3392e33e4b16e422b882009c4e37b5be3d40d))
* **chat:** serve index.html with Cache-Control: no-cache ([#473](https://github.com/mdopp/solarisbay/issues/473)) ([60bdcfa](https://github.com/mdopp/solarisbay/commit/60bdcfa5435ac8ea721632f741f9e53beb296c9d))
* **chat:** sort skills alphabetically + open setting cards from their top ([#472](https://github.com/mdopp/solarisbay/issues/472)) ([666e379](https://github.com/mdopp/solarisbay/commit/666e3797deda600adabafedcdd7c31a54e15475f))
* **chat:** stop setting cards shrinking + sort the slash list alphabetically ([#479](https://github.com/mdopp/solarisbay/issues/479)) ([734f346](https://github.com/mdopp/solarisbay/commit/734f3468ba7255346325b1c5956e6c15b9295cff))
* **engine:** resolve guessed entity_ids so history isn't a false "never" ([#467](https://github.com/mdopp/solarisbay/issues/467)) ([f63d14d](https://github.com/mdopp/solarisbay/commit/f63d14d94187b7b8e38b81bf9417ea8ac169a472))
* **engine:** surface ambient sensors so Solaris finds room temperature ([#462](https://github.com/mdopp/solarisbay/issues/462)) ([73dc499](https://github.com/mdopp/solarisbay/commit/73dc49971cdf2c0c3959233a046f3443e7395c38))
* **skill:** emit the actual web-search URLs, not just a promise of a link ([#489](https://github.com/mdopp/solarisbay/issues/489)) ([52fa029](https://github.com/mdopp/solarisbay/commit/52fa0292d5cce2d30bfa68b252d506d2568029f9))
* **template:** make the chat proxy host SSE-friendly for long mobile streams ([d6f0810](https://github.com/mdopp/solarisbay/commit/d6f08106994711bace7c231865792c7fdde9ccf3)), closes [#452](https://github.com/mdopp/solarisbay/issues/452)
* **template:** SSE-friendly chat proxy host for long mobile streams ([1c6b40f](https://github.com/mdopp/solarisbay/commit/1c6b40f6b96b2dbff3e20807bb892d49b04ce090))

## [0.14.0](https://github.com/mdopp/solarisbay/compare/v0.13.0...v0.14.0) (2026-06-15)


### Features

* **chat:** /persona and /model commands, drop Settings button + persona dropdown ([788e840](https://github.com/mdopp/solarisbay/commit/788e8405ceda50276aa5a06d18bf152d221993c7)), closes [#420](https://github.com/mdopp/solarisbay/issues/420)
* **chat:** structured LLM-call trace modal + usable skill/soul editor ([dae3e63](https://github.com/mdopp/solarisbay/commit/dae3e63cacecc86ba32a7046da4bba3f7338c146)), closes [#416](https://github.com/mdopp/solarisbay/issues/416) [#418](https://github.com/mdopp/solarisbay/issues/418)
* **chat:** wire dynamic-skills promotion to the generic SB approval API ([5a0f6ce](https://github.com/mdopp/solarisbay/commit/5a0f6ce98296b368cc34cd6f2f24b704c143efa9)), closes [#427](https://github.com/mdopp/solarisbay/issues/427)
* dynamic-skills SB-approval promotion + custom Solaris wake-word wiring ([e1b78f2](https://github.com/mdopp/solarisbay/commit/e1b78f28d3d91586a876519ef266267982982549))
* **template:** wire custom "Solaris" wake word + ship model-gen recipe ([9868db6](https://github.com/mdopp/solarisbay/commit/9868db682d05e0640171afb136b5302a142cb5c3)), closes [#407](https://github.com/mdopp/solarisbay/issues/407)


### Bug Fixes

* **chat:** household slash commands, /help parity, single durable household session ([9281c3d](https://github.com/mdopp/solarisbay/commit/9281c3df5b77873ce42fcd8b3f5970f92a496f22)), closes [#421](https://github.com/mdopp/solarisbay/issues/421) [#417](https://github.com/mdopp/solarisbay/issues/417) [#419](https://github.com/mdopp/solarisbay/issues/419)
* **db:** seed debug_mode from SOLARIS_DEBUG_MODE_DEFAULT env ([ffc9748](https://github.com/mdopp/solarisbay/commit/ffc9748ed4c92df3404642dab76b66bb1bec0388)), closes [#432](https://github.com/mdopp/solarisbay/issues/432)
* **engine:** containment-check skill promotion path against symlink escape ([7f7837e](https://github.com/mdopp/solarisbay/commit/7f7837e4397f2f9f358963def6c360998e278148))
* **engine:** containment-check skill promotion path against symlink escape ([b0cbfa4](https://github.com/mdopp/solarisbay/commit/b0cbfa472bb063f0de411a79d362628761e5b92c)), closes [#439](https://github.com/mdopp/solarisbay/issues/439)
* **gatekeeper:** gate enrolment listing and scope session delete by owner ([78a21fb](https://github.com/mdopp/solarisbay/commit/78a21fbe2e437421a19efdeee6e5ba2e70bfc3c0)), closes [#437](https://github.com/mdopp/solarisbay/issues/437) [#438](https://github.com/mdopp/solarisbay/issues/438)
* **gatekeeper:** serialise concurrent same-uid enroll capture ([5a3bbb0](https://github.com/mdopp/solarisbay/commit/5a3bbb0f9e23772abf5361976168684765efeaf9)), closes [#441](https://github.com/mdopp/solarisbay/issues/441)
* post-Hermes skill cleanup + debug_mode default from env ([e2a3357](https://github.com/mdopp/solarisbay/commit/e2a3357f708db2acadce55b8994a3e6319273450))
* **security:** auth-gate /enrolments + owner-gate session delete ([#437](https://github.com/mdopp/solarisbay/issues/437), [#438](https://github.com/mdopp/solarisbay/issues/438)) ([0e3cb56](https://github.com/mdopp/solarisbay/commit/0e3cb562826b483ae0107aa2f1fbec6d987b67db))
* **skill:** replace stale Hermes tool names and drop dead hermes-api check ([91c001d](https://github.com/mdopp/solarisbay/commit/91c001df059128d3a25ec9076f2b28511c65e7dc)), closes [#431](https://github.com/mdopp/solarisbay/issues/431) [#433](https://github.com/mdopp/solarisbay/issues/433)
* **skill:** update admin-diagnose and admin-logs container name map ([c35ddc7](https://github.com/mdopp/solarisbay/commit/c35ddc7a8f4a028613a8b50ccff42769765e9042)), closes [#430](https://github.com/mdopp/solarisbay/issues/430)
* **template:** drop {{VAR}} comment false-positive, clarify HA token absence ([9b733dd](https://github.com/mdopp/solarisbay/commit/9b733dd3e6f213dca0fd96f0b07bc6f134384090)), closes [#424](https://github.com/mdopp/solarisbay/issues/424) [#425](https://github.com/mdopp/solarisbay/issues/425)
* **template:** forward GATEKEEPER_URL and PUSH_TOKEN to the chat container ([912eb95](https://github.com/mdopp/solarisbay/commit/912eb95ca92f351ca2795753fafd0104308b57bf)), closes [#440](https://github.com/mdopp/solarisbay/issues/440)
* **template:** robustify HA long-lived token adoption ([6c40065](https://github.com/mdopp/solarisbay/commit/6c4006573071a87bb11d6938437b0d8f21b91fdd))
* **template:** robustify HA long-lived token adoption ([717b9f5](https://github.com/mdopp/solarisbay/commit/717b9f576b364f28d17ad6e76c8b2d11b15f7a42)), closes [#425](https://github.com/mdopp/solarisbay/issues/425)
* wire gatekeeper push into chat container + serialise enroll capture ([b519532](https://github.com/mdopp/solarisbay/commit/b519532e36684bbc2c822d923e20d2c8dd8e3e1d))

## [0.13.0](https://github.com/mdopp/solbay/compare/v0.12.1...v0.13.0) (2026-06-15)


### Features

* **chat:** anchor the transcript to the bottom, fix mobile keyboard gap ([c22a04c](https://github.com/mdopp/solbay/commit/c22a04cbbf57cbd008fef859b07c57a1b0cecbf7)), closes [#412](https://github.com/mdopp/solbay/issues/412)
* **chat:** graphical VRAM headroom from ServiceBay's real GPU numbers ([#401](https://github.com/mdopp/solbay/issues/401)) ([8f9fd5b](https://github.com/mdopp/solbay/commit/8f9fd5b0e308413a3857a6e736b416ac8b17dbbb))
* **chat:** guide self-enrolment by name not technical uid ([#399](https://github.com/mdopp/solbay/issues/399)) ([2270373](https://github.com/mdopp/solbay/commit/2270373cae754118514eb8dc68a6e88ab571894d)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **chat:** move settings + search into inline command cards, merge mobile header ([0be8424](https://github.com/mdopp/solbay/commit/0be8424c4f9a036a70bb9c2102b04a8d87762292)), closes [#410](https://github.com/mdopp/solbay/issues/410) [#411](https://github.com/mdopp/solbay/issues/411)
* **chat:** self-service voice enrolment for the household profile ([#397](https://github.com/mdopp/solbay/issues/397)) ([10e9013](https://github.com/mdopp/solbay/commit/10e90137e4e3411d70c34dddba1b9a0362806c84)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **chat:** voice onboarding reliability and chat-UI command-card consolidation ([1fa1717](https://github.com/mdopp/solbay/commit/1fa171748a3686f614a967d729f3aa8f6389f377))
* **skill:** pin the self-enrolment step order in the household soul ([#400](https://github.com/mdopp/solbay/issues/400)) ([10071e6](https://github.com/mdopp/solbay/commit/10071e61f2ee02f382c3e6413caef4b7a6cc6fcb)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **skill:** rename the household persona to Solaris, enrol on sentences ([#403](https://github.com/mdopp/solbay/issues/403)) ([f99659b](https://github.com/mdopp/solbay/commit/f99659b8c5952506371630d409e64b4573663b60)), closes [#396](https://github.com/mdopp/solbay/issues/396)
* **solaris:** rebrand stage 2 — coordinated repo-wide artifact rename + draft BRAND.md ([75f3818](https://github.com/mdopp/solbay/commit/75f3818c98e87ee2c05d915ebf33d8ad09cc2d12))
* **solaris:** rebrand stage 2 — rename artifacts solilos→solaris, solbay→solarisbay ([1b6530a](https://github.com/mdopp/solbay/commit/1b6530a0aef4bdd7ae46ad77d0d37b4347947be6)), closes [#408](https://github.com/mdopp/solbay/issues/408)
* **template:** switch the household fast model to gemma4:e4b ([#402](https://github.com/mdopp/solbay/issues/402)) ([ccb197c](https://github.com/mdopp/solbay/commit/ccb197ce46301f26a1c8bdc6c826b78c47fbfa19))


### Bug Fixes

* **chat:** fold the turn trace into one step list, drop the redundant block ([102bc86](https://github.com/mdopp/solbay/commit/102bc868e28d086c3e308fd7700f3713bf777c9b)), closes [#406](https://github.com/mdopp/solbay/issues/406)
* **chat:** hand the model the exact 3-sentence enrol prompt to echo ([f4de2d6](https://github.com/mdopp/solbay/commit/f4de2d6865a55cc1ab105a364ae246c5b5369de3)), closes [#404](https://github.com/mdopp/solbay/issues/404)
* **chat:** persist a voice turn's trace into its durable Zuhause session ([f6c0979](https://github.com/mdopp/solbay/commit/f6c0979256c69393e44efe7503d87177efc2dc7d)), closes [#405](https://github.com/mdopp/solbay/issues/405)
* **chat:** remove the activity bubble on a stopped turn ([f1fc218](https://github.com/mdopp/solbay/commit/f1fc218a4feec42307feb6a8b541a808a0bace5b))
* **chat:** remove the live activity bubble on a stopped turn ([d411611](https://github.com/mdopp/solbay/commit/d411611be3f6e116b0eeb0465173d2d47f3883ae)), closes [#414](https://github.com/mdopp/solbay/issues/414)
* **template:** wait for gatekeeper Wyoming STT before wiring it as Assist STT ([8aaee5e](https://github.com/mdopp/solbay/commit/8aaee5e2821d339ce70f7f0bdf0f0baaa819a2d1)), closes [#395](https://github.com/mdopp/solbay/issues/395)

## [0.12.1](https://github.com/mdopp/solbay/compare/v0.12.0...v0.12.1) (2026-06-13)


### Bug Fixes

* **chat:** surface enroll STATUS_FAILED honestly and clear the stale row ([f36fe38](https://github.com/mdopp/solbay/commit/f36fe3841429d7956f6a01935dcfcd786d223d48)), closes [#389](https://github.com/mdopp/solbay/issues/389)
* **chat:** two eval-found bugfixes — admin MCP panel + honest enroll failure ([ba9553a](https://github.com/mdopp/solbay/commit/ba9553a4ab17dbe49e6d0ebbf31557b4bd452d5e))
* **chat:** unwrap CombinedToolbox in admin MCP panel introspection ([3694067](https://github.com/mdopp/solbay/commit/36940676ec11d6df50c57aa908b1bcc590ee3658)), closes [#390](https://github.com/mdopp/solbay/issues/390)

## [0.12.0](https://github.com/mdopp/solbay/compare/v0.11.0...v0.12.0) (2026-06-13)


### Features

* add a global Kokoro voice picker like the model picker ([453ecd1](https://github.com/mdopp/solbay/commit/453ecd1038755eb593a68735f3aa07827589ef7b)), closes [#368](https://github.com/mdopp/solbay/issues/368)
* answer "wer bin ich?" from the resident voice-ID belief ([2ab8ec5](https://github.com/mdopp/solbay/commit/2ab8ec527200571066b4b6b1854078c8490a917f))
* answer "wer bin ich" from the voice-ID belief ([372090d](https://github.com/mdopp/solbay/commit/372090dafeb0150cb8aaf4980fe1cff5b9acd984)), closes [#384](https://github.com/mdopp/solbay/issues/384)
* **chat:** confirm home-securing actions, act decisively otherwise ([971585d](https://github.com/mdopp/solbay/commit/971585d3364b5c6ccd0f41ab0904d75dbb1668fa)), closes [#382](https://github.com/mdopp/solbay/issues/382)
* **chat:** HA service legend in registry + home-securing confirmation policy ([a96996b](https://github.com/mdopp/solbay/commit/a96996b5df4065e7f8c49680804f5c358286669d))
* **chat:** HA state-history search and list/run scenes scripts automations ([0230b5d](https://github.com/mdopp/solbay/commit/0230b5d262440fc98100692870c03de6dda120b9)), closes [#369](https://github.com/mdopp/solbay/issues/369) [#370](https://github.com/mdopp/solbay/issues/370)
* **chat:** handle denied access requests — drop biometric, provision nothing ([6f12e7f](https://github.com/mdopp/solbay/commit/6f12e7f9d69b2e01cf94c195736e68f01dde1324))
* **chat:** inject per-domain HA service legend into the entity registry ([20d120c](https://github.com/mdopp/solbay/commit/20d120c8012fec50e0d999bee1217a360f0e7cfe)), closes [#381](https://github.com/mdopp/solbay/issues/381)
* **chat:** make the household profile model admin-selectable in the picker ([a7003f5](https://github.com/mdopp/solbay/commit/a7003f5d01d52e940ac8b2e1e40ecda2f44d0dbd)), closes [#366](https://github.com/mdopp/solbay/issues/366)
* **chat:** onboarding approval + provisioning on SB access-request MCP ([9ca171f](https://github.com/mdopp/solbay/commit/9ca171f62d41a889a3c5b11141bcf74ebb8925ea))
* **chat:** onboarding approval + provisioning on SB access-request MCP ([10d605b](https://github.com/mdopp/solbay/commit/10d605b6d48e07994156b5be45429c7eb84d5b1c)), closes [#355](https://github.com/mdopp/solbay/issues/355)
* **chat:** panel model management + Kokoro voice picker ([846f119](https://github.com/mdopp/solbay/commit/846f119c42d04d5818ee5bfef62da2fddf6288bb))
* **chat:** registration flow — enrol voice + file pending resident request ([b4c3d80](https://github.com/mdopp/solbay/commit/b4c3d80bbf4f758cc8735d7fb73e502bc2b67a88))
* **chat:** registration flow — enrol voice + file pending resident request ([38f2de2](https://github.com/mdopp/solbay/commit/38f2de2f76b8a9c4ed97019d3b3a7941ac0dcd67)), closes [#376](https://github.com/mdopp/solbay/issues/376)
* **chat:** voice-enrolment tool wrapping gatekeeper POST /enrol ([0cd9cc9](https://github.com/mdopp/solbay/commit/0cd9cc92452c947b6e3944a1329f5d7994e2d6e3))
* **chat:** voice-enrolment tool wrapping gatekeeper POST /enrol ([0cbcab9](https://github.com/mdopp/solbay/commit/0cbcab9ceefa927f919066dece29f9f9ae4168bc)), closes [#364](https://github.com/mdopp/solbay/issues/364)
* **engine:** personalize turn prompt with the resident identity ([f2876dc](https://github.com/mdopp/solbay/commit/f2876dcdfac9f40c76e927cb585bc8e5490957db)), closes [#352](https://github.com/mdopp/solbay/issues/352)
* **gatekeeper:** reverse enroll-stash — live-voice onboarding capture ([7c508ca](https://github.com/mdopp/solbay/commit/7c508caeb46bbfafc459b7bcfacfc0de426dd1b7))
* **gatekeeper:** reverse enroll-stash for live-voice onboarding capture ([cfb446a](https://github.com/mdopp/solbay/commit/cfb446a54029bdc379c7847712620fd5952aed24))
* pull Ollama models from the panel + VRAM headroom estimate ([40410f5](https://github.com/mdopp/solbay/commit/40410f5ee2a34e0b1b9001ef49567e11c0aacb05)), closes [#367](https://github.com/mdopp/solbay/issues/367)
* resident-personalized turns, trace tool-step fix, HA history+run tools, admin-selectable household model ([ac709c1](https://github.com/mdopp/solbay/commit/ac709c1346e58d0512a3e4587b87bbf3ec133da4))
* route an unknown speaker to the guest profile ([5f14ae9](https://github.com/mdopp/solbay/commit/5f14ae9db6b151d74f3883bda1a1be9a125dca02))
* route an unknown speaker to the guest profile ([ea85395](https://github.com/mdopp/solbay/commit/ea853951c4e728fa602a6789e9025369194e0bb5)), closes [#351](https://github.com/mdopp/solbay/issues/351)
* **skill:** add the resident-registration onboarding dialog ([e9a4b1d](https://github.com/mdopp/solbay/commit/e9a4b1d07b0fe1606c3cfd16bf9ecf8927ad613e)), closes [#354](https://github.com/mdopp/solbay/issues/354)
* **skill:** guest-greeting onboarding dialog for unknown speakers ([f1ca148](https://github.com/mdopp/solbay/commit/f1ca14810dcaf9d82ccc3ca5e504bceae98d023a))
* **skill:** guest-greeting onboarding dialog for unknown speakers ([3d50acf](https://github.com/mdopp/solbay/commit/3d50acf143f91eb5c20fa4be72fa020b779d366d)), closes [#375](https://github.com/mdopp/solbay/issues/375)
* **skill:** resident-registration onboarding dialog ([ca96960](https://github.com/mdopp/solbay/commit/ca969604e251458752fd788c2614a6051b336c7e))
* **voice:** wire gatekeeper speaker-ID into the live Assist path ([#350](https://github.com/mdopp/solbay/issues/350), approach b) ([#362](https://github.com/mdopp/solbay/issues/362)) ([d556247](https://github.com/mdopp/solbay/commit/d5562474be2dd96464aace3d3df8c53a5966e560))


### Bug Fixes

* **chat:** drop alembic import from the pending-residents test ([efcb9d8](https://github.com/mdopp/solbay/commit/efcb9d843ef16d813c78a2c514a1fb08a4f0526e))
* **chat:** map natural cover service names to HA open_cover/close_cover/stop_cover ([90211d5](https://github.com/mdopp/solbay/commit/90211d550b44086b6d8efcc5a3bff5f47ef44020))
* **chat:** normalize natural cover service names to HA's open_cover/close_cover/stop_cover ([88045a6](https://github.com/mdopp/solbay/commit/88045a62356a0a472fe7607313e7b2cd6ef38358)), closes [#379](https://github.com/mdopp/solbay/issues/379)
* **chat:** render persisted tool trace steps as the tool, expand by default ([1167276](https://github.com/mdopp/solbay/commit/116727676ae51b4b7a002a9464dbe8cdf6a4a993)), closes [#371](https://github.com/mdopp/solbay/issues/371)
* **db:** chain the pending-resident-request migration after enroll_requests (0015) ([7c32b8f](https://github.com/mdopp/solbay/commit/7c32b8f12bceb0bdc03523a620c1156c34872de4))

## [0.11.0](https://github.com/mdopp/solbay/compare/v0.10.0...v0.11.0) (2026-06-12)


### Features

* **chat:** alarms ring a sound instead of speaking ([040d203](https://github.com/mdopp/solbay/commit/040d2035dc1321f892abeac093617fe05a170018)), closes [#348](https://github.com/mdopp/solbay/issues/348)
* **chat:** durable household voice session + live browser mirror ([7a46fad](https://github.com/mdopp/solbay/commit/7a46fad19f95402ffe987f3a9ca810482e655fae)), closes [#345](https://github.com/mdopp/solbay/issues/345) [#344](https://github.com/mdopp/solbay/issues/344)
* **chat:** guest profile — ephemeral Q&A plus basic home control ([e2a1005](https://github.com/mdopp/solbay/commit/e2a10059eb96a7d6ccf0ba56759551540cf51815)), closes [#353](https://github.com/mdopp/solbay/issues/353)
* **chat:** live collapsed activity bubble for a turn's llm and tool steps ([8790976](https://github.com/mdopp/solbay/commit/879097669c8df9d7d23debdc6facb0a38a0f8570))
* **chat:** live collapsed activity bubble for a turn's llm and tool steps ([63d6f5e](https://github.com/mdopp/solbay/commit/63d6f5e3cc1b4bd610b2da674ea591eae31efc93)), closes [#347](https://github.com/mdopp/solbay/issues/347)
* **chat:** record tool executions as interleaved trace steps with timings ([c0b782e](https://github.com/mdopp/solbay/commit/c0b782e7ff06ddb25881fa58319dbe7a4d8f9259)), closes [#346](https://github.com/mdopp/solbay/issues/346)
* **template:** prefer the Martin TTS bridge in the Sol pipeline ([94031f3](https://github.com/mdopp/solbay/commit/94031f3f5b6ca2b9db1ccf103357cd8ba36a199d))
* **tts:** add the solilos-tts image — Kokoro-Martin German voice on GPU ([f6c8af0](https://github.com/mdopp/solbay/commit/f6c8af0a002409eb4af99ff3f84a9bec860bd320))


### Bug Fixes

* **chat:** catch perfect-tense and passive German device-action fabrications ([41c2da3](https://github.com/mdopp/solbay/commit/41c2da32128896d64bf740ce12dac2ba491b13bd))
* **chat:** catch perfect-tense device-action fabrications in the guard ([3ce1eb2](https://github.com/mdopp/solbay/commit/3ce1eb2d01f81abab74ebecc7bb816acd411514f)), closes [#360](https://github.com/mdopp/solbay/issues/360)
* **chat:** discipline last, stream-safe contextvar, uid into tool tasks ([235a4b2](https://github.com/mdopp/solbay/commit/235a4b29b150941c22b71358d9033998a7ad54e4))
* **chat:** force the tool pass when a turn fabricates a device-action claim ([afc156d](https://github.com/mdopp/solbay/commit/afc156d7f00ab334a48d9a72934e5abdc3ed076e)), closes [#356](https://github.com/mdopp/solbay/issues/356)
* **chat:** pin the tool-discipline rule at the end of the system block ([93aa7a0](https://github.com/mdopp/solbay/commit/93aa7a00fc0e7127df6ca089e49588354384ea82))
* **chat:** run the household profile at low temperature ([a293326](https://github.com/mdopp/solbay/commit/a293326e6977b94a872995bd472a05fc8c2182c4))
* **skill:** never narrate a device action — call the tool in this turn ([c6fb7a2](https://github.com/mdopp/solbay/commit/c6fb7a2070d8cba50c5beaf45164bdbaeea4482d))
* **template:** converge the pipeline on tts_voice drift too ([47581d1](https://github.com/mdopp/solbay/commit/47581d1f65e9c8702208f0f9e3570cb425c59b80))
* **template:** the Martin bridge voice is martin, not kokoro ([7438ad5](https://github.com/mdopp/solbay/commit/7438ad570ce27e72f8bf96ea2667c91793909e25))
* **template:** warm-load from the locally installed tags ([dcc8d25](https://github.com/mdopp/solbay/commit/dcc8d25988eaab02311518c9a5e48e8dad709b08)), closes [#339](https://github.com/mdopp/solbay/issues/339)
* **template:** warm-load small models first — order decides co-residency ([432e0f3](https://github.com/mdopp/solbay/commit/432e0f312f5f0dd31ff78e607d9ef975fde4053e)), closes [#340](https://github.com/mdopp/solbay/issues/340)
* **template:** warm-load the chat models after every ollama deploy ([d25a287](https://github.com/mdopp/solbay/commit/d25a287199f74e202d4ec5d768e07ebab4fe231e))

## [0.10.0](https://github.com/mdopp/solbay/compare/v0.9.0...v0.10.0) (2026-06-12)


### Features

* **chat:** add the Sol Engine core replacing the Hermes gateways ([3542115](https://github.com/mdopp/solbay/commit/35421153b8b93559d4c004e51f0a3df585520596))
* **chat:** complete the Sol Engine — facade, crons, admin MCP, Hermes retired ([34da56f](https://github.com/mdopp/solbay/commit/34da56f8f0a3e9eea1cf368e1d7fdd51fa4c15cb))
* **chat:** decouple everyday-chat model preference from persona via settings toggle ([eb711e3](https://github.com/mdopp/solbay/commit/eb711e3a255e38e0ac27038d5e895812e79872f0))
* **chat:** move the soul to the chat-owned volume with direct panel writes ([cebeca8](https://github.com/mdopp/solbay/commit/cebeca84c384cc5c5631fd4e3024988d7a0708fd))
* **db:** add engine cron run stamps table ([33a8ab4](https://github.com/mdopp/solbay/commit/33a8ab4dd176b534d9406d6fa5cef2bef7633f9a))
* **db:** add engine session, message and timer tables ([08925df](https://github.com/mdopp/solbay/commit/08925df14556a795044961944cf96108ed23b807))
* **gatekeeper:** speak the engine facade instead of Hermes sessions ([79fd562](https://github.com/mdopp/solbay/commit/79fd5629a7ab679fb7d57bbb07efc553c4ee8be1))
* Sol Engine Phase 0+1 — native agent core replaces the Hermes gateways ([e492e1c](https://github.com/mdopp/solbay/commit/e492e1c0573cea35b6829bc5e032ea0e0562194c))
* **template:** engine-only solilos pod with HA voice-pipeline wiring ([cfa4eb0](https://github.com/mdopp/solbay/commit/cfa4eb0a3a6fbc7af2cb15d046e4dc65af186f28))
* **template:** keep all three models resident with a right-sized 32k context ([9ecd96d](https://github.com/mdopp/solbay/commit/9ecd96d65e972c736d89e75b79e45c44b3b7e02f))
* **template:** wire the chat container for the Sol Engine ([08bd478](https://github.com/mdopp/solbay/commit/08bd478c20907a0adf866679a79f5226d73401cd))


### Bug Fixes

* **chat:** target every assist satellite when a timer announces ([d25643c](https://github.com/mdopp/solbay/commit/d25643c4e3ecfa89a0c7be1122774dd4aca78189))
* **template:** align the GPU render-path defaults with the v7 residency values ([a8364b5](https://github.com/mdopp/solbay/commit/a8364b51aae8831042e2bd5730acfff4d82b67b4))
* **template:** box-verified voice-wiring fixes and an abort-path guard ([f303bbe](https://github.com/mdopp/solbay/commit/f303bbefd9a699c6d2047f5486701814d8f2f223))
* **template:** pin the pipeline to the wyoming engines and both PE selects ([4279b65](https://github.com/mdopp/solbay/commit/4279b65e55383d71e6656574942d4554c44b9939))
* **template:** retry async HA setup races in the voice wiring ([1459462](https://github.com/mdopp/solbay/commit/14594621aea1e95944a2c12979eaa7dc217beffb))

## [0.9.0](https://github.com/mdopp/solbay/compare/v0.8.1...v0.9.0) (2026-06-10)


### Features

* **chat:** route the Sol Gründlich persona to the sol-deep gateway ([ec6ab02](https://github.com/mdopp/solbay/commit/ec6ab021e47b59f4bf619f797c514f167e73b28e))
* Sol Gründlich — the Sol identity on 12b for thorough chat + crons ([8aa3b61](https://github.com/mdopp/solbay/commit/8aa3b612f383e5e0dbff4d375b126b3c4cad0d0a))
* **template:** provision a sol-deep Hermes profile on 12b for the Gründlich mode and crons ([0039880](https://github.com/mdopp/solbay/commit/00398808c8532a3dc00e51148a6fe146637019d3))

## [0.8.1](https://github.com/mdopp/solbay/compare/v0.8.0...v0.8.1) (2026-06-10)


### Bug Fixes

* chat turns ≤2s — stop fast-model thinking + keep chat/embed models resident ([1e750a7](https://github.com/mdopp/solbay/commit/1e750a7f580da77c621235bc12b3df4a75330635))
* **chat:** stop the fast model thinking on every turn so chat turns are sub-2s ([19b03be](https://github.com/mdopp/solbay/commit/19b03bed0110cd7fc2f9ebc4ca330fd16fb5c695))
* **template:** drop session_search + todo from the household toolset prefill ([1f1d9c6](https://github.com/mdopp/solbay/commit/1f1d9c6b80b1e2237dbda4bc2d565b419635ab18))
* **template:** drop session_search + todo from the household toolset prefill ([bd2abb4](https://github.com/mdopp/solbay/commit/bd2abb4a604a65cc79068290d4a68e34ac1fc9ac))
* **template:** keep chat + embed models resident with OLLAMA_MAX_LOADED_MODELS=2 ([2b0a8a2](https://github.com/mdopp/solbay/commit/2b0a8a2d6f5f4931c53014a0cc6a5f646286f8d1))
* **template:** remove already-seeded bundled skills from the household home ([a4d1f76](https://github.com/mdopp/solbay/commit/a4d1f76976c264d2cc9fcccd203241264e03c378))
* **template:** remove already-seeded bundled skills from the household home ([a7cf447](https://github.com/mdopp/solbay/commit/a7cf44780ed8491e2a77aabe1959ecf82c9c97e3))

## [0.8.0](https://github.com/mdopp/solbay/compare/v0.7.0...v0.8.0) (2026-06-10)


### Features

* **chat:** tag each LLM trace step with its Hermes profile ([e8021fd](https://github.com/mdopp/solbay/commit/e8021fda34cf8dbab1b22c050bf2dfe703862242))
* **chat:** tag each LLM trace step with its Hermes profile ([c0956d5](https://github.com/mdopp/solbay/commit/c0956d555416a5c09b3f4b1b595bdcca9512d6e8))


### Bug Fixes

* **chat:** keepalive heartbeat so tool-turn answers survive the long Ollama prefill ([51b5419](https://github.com/mdopp/solbay/commit/51b54195585186fa9880b02f2846df032e1340c9))
* **chat:** keepalive the SSE stream through the tool round-trip so the answer renders ([7150559](https://github.com/mdopp/solbay/commit/7150559847af013de9a0ac0cb5b80f116aa13854)), closes [#319](https://github.com/mdopp/solbay/issues/319)
* **template:** make install_gpu_quadlet_fallback activation-idempotent ([761a0fd](https://github.com/mdopp/solbay/commit/761a0fd6efac3e48cdcd43221378cb2708450bb4)), closes [#322](https://github.com/mdopp/solbay/issues/322)
* **template:** make ollama GPU-quadlet fallback activation-idempotent so redeploys keep the GPU ([8eff436](https://github.com/mdopp/solbay/commit/8eff4360e38c1bd6fb860eb827796189ac5a214d))

## [0.7.0](https://github.com/mdopp/solbay/compare/v0.6.0...v0.7.0) (2026-06-09)


### Features

* always-on Ollama trace proxy — permanent LLM traceability (phase 1) ([a7904d6](https://github.com/mdopp/solbay/commit/a7904d6e9cf328a8ad9910144a3d87f2140c125c))
* **chat:** hide internal hint prefixes in history + Wiederholen re-run ([7737c61](https://github.com/mdopp/solbay/commit/7737c61981c0a0bcceb76a6e5df3fbab1aa9efc6)), closes [#309](https://github.com/mdopp/solbay/issues/309) [#308](https://github.com/mdopp/solbay/issues/308)
* **gatekeeper:** trim MCP prefill noise — suppress empty FastMCP capabilities and drop gatekeeper-mcp from the household profile ([f129284](https://github.com/mdopp/solbay/commit/f129284bb63a889e840944b6639f2338e51b0c48)), closes [#312](https://github.com/mdopp/solbay/issues/312) [#313](https://github.com/mdopp/solbay/issues/313)
* household runtime batch — SOUL HA grounding, prefill curation, trace detail endpoint ([1c37e52](https://github.com/mdopp/solbay/commit/1c37e524af87d37e5f756413e7dd99362c4613a1))
* **template:** always-on Ollama trace proxy for permanent LLM traceability ([2098390](https://github.com/mdopp/solbay/commit/20983908e0d2bb61dd25196d397d812e59b930e5))
* **template:** curate household default profile — drop servicebay-mcp + bundled skills from the first-turn prefill ([c42f691](https://github.com/mdopp/solbay/commit/c42f69167f7e3356af20cd99e98078dbf24a0f64)), closes [#292](https://github.com/mdopp/solbay/issues/292)
* **template:** hide internal hint prefixes in chat history + add Wiederholen re-run ([dd64ec3](https://github.com/mdopp/solbay/commit/dd64ec3ccd389d494113243d1d4e8238f00fbdb9))
* **template:** per-turn LLM-step trace panel in the chat UI ([6054cca](https://github.com/mdopp/solbay/commit/6054cca56a493207cbaa8cb4f128a125b1c820e8))
* **template:** per-turn LLM-step trace panel in the chat UI ([675a554](https://github.com/mdopp/solbay/commit/675a554e9095ca022ce955aea28f3d91326162d4)), closes [#307](https://github.com/mdopp/solbay/issues/307)
* **template:** persist per-message LLM trace and serve it reopen-consistently ([0fc6786](https://github.com/mdopp/solbay/commit/0fc678697e8ea8ec523c3563fdf2dd4cdf6348b8)), closes [#306](https://github.com/mdopp/solbay/issues/306)
* **template:** serve exact per-call trace content at /__traces__/&lt;id&gt; ([58797c8](https://github.com/mdopp/solbay/commit/58797c829e1018e7bcf6099bd32876a60fd5c739)), closes [#305](https://github.com/mdopp/solbay/issues/305)
* **template:** trace persistence, SOUL.md bind-mount, household MCP trim ([f933103](https://github.com/mdopp/solbay/commit/f9331033d5997878266a213bbfd2b48da8852103))


### Bug Fixes

* **chat:** retry session create on a title collision instead of (no reply) ([b45a595](https://github.com/mdopp/solbay/commit/b45a5957ca32489b256020519157d5c798e25f7a))
* **chat:** retry session create on title collision — household (no reply) ([#301](https://github.com/mdopp/solbay/issues/301)) ([bb44b0b](https://github.com/mdopp/solbay/commit/bb44b0bf0f7a560735603a8827f73cc2e72d3b22))
* **ci:** make release-please reliably trigger the image build for the tag ([7979a5f](https://github.com/mdopp/solbay/commit/7979a5fabed2ee6a44d83898bf76e0c236c736bc))
* **ci:** make release-please reliably trigger the tag image build ([ccc334b](https://github.com/mdopp/solbay/commit/ccc334b4769910f959cf5a08ac5f6bd18865c453))
* **template:** keep the admin gateway up across reboots ([d70b18b](https://github.com/mdopp/solbay/commit/d70b18b60885a798260294c46b5a54af14e08432))
* **template:** keep the admin gateway up across reboots ([#299](https://github.com/mdopp/solbay/issues/299)) ([c7f36e2](https://github.com/mdopp/solbay/commit/c7f36e279392bf5752091fb42b11a59504dd1c34))
* **template:** point Hermes at the trace proxy permanently via the container env ([b28315a](https://github.com/mdopp/solbay/commit/b28315aeb33bffd51ef005a86c08588cde67f175))
* **template:** read HA states entity-by-entity so Sol stops reporting all-off ([fea9413](https://github.com/mdopp/solbay/commit/fea9413e45ae9103283ef086bd90a74e51adc3fe)), closes [#289](https://github.com/mdopp/solbay/issues/289)
* **template:** render trace step-detail from nested request/response shape ([fe3476f](https://github.com/mdopp/solbay/commit/fe3476f08a8040473f6a96e8030f6a71fb50afbb))
* **template:** renderTraceDetail reads nested request/response trace shape ([69fdea7](https://github.com/mdopp/solbay/commit/69fdea7940549aacbb8c572acb3c6775694962f3)), closes [#316](https://github.com/mdopp/solbay/issues/316)
* **template:** route Hermes through the trace proxy permanently (read provider from container env) ([2461ee0](https://github.com/mdopp/solbay/commit/2461ee0ddda86b00fa0d8e9f0cdd6a1f18beb15a))
* **template:** ship SOUL.md via the container bind-mount so post-deploy actually installs it ([f10a906](https://github.com/mdopp/solbay/commit/f10a9065bb9bb8b72e228f924045acb3fa3a0d1b)), closes [#311](https://github.com/mdopp/solbay/issues/311)

## [0.6.0](https://github.com/mdopp/solbay/compare/v0.5.0...v0.6.0) (2026-06-09)


### Features

* **solilos-chat:** route chat turns to the household or admin Hermes gateway ([c4872c7](https://github.com/mdopp/solbay/commit/c4872c77f543613d518a04e4478c97d5c759e6f3)), closes [#293](https://github.com/mdopp/solbay/issues/293)
* **template:** instance-per-profile Hermes — household + admin gateway containers ([75adeb4](https://github.com/mdopp/solbay/commit/75adeb4b3df449ac3e877dd401ce4b722fac9982)), closes [#293](https://github.com/mdopp/solbay/issues/293)
* **template:** multi-profile Hermes — household + admin gateways per profile ([2d83c16](https://github.com/mdopp/solbay/commit/2d83c162977d6c8cc0af914ce1a28eec43ec8a0b))
* **template:** multi-profile Hermes — household + isolated admin gateway in one container ([#293](https://github.com/mdopp/solbay/issues/293)) ([f30dead](https://github.com/mdopp/solbay/commit/f30dead91f0f501c9013387f088f08ac86738a47))
* **template:** multi-profile Hermes via one container, household=default + admin secondary ([b03297f](https://github.com/mdopp/solbay/commit/b03297f690201b423e3735089705a2eb61ce3e99))
* **template:** pin voice gatekeeper to the household Hermes gateway ([2159088](https://github.com/mdopp/solbay/commit/2159088ba4b7cc120570cbd6970727504dc3aa66)), closes [#293](https://github.com/mdopp/solbay/issues/293)
* **template:** provision household + admin Hermes profiles in post-deploy ([c40276e](https://github.com/mdopp/solbay/commit/c40276ee355fa581d77a9460e0ba0bc3dd13e1b4)), closes [#293](https://github.com/mdopp/solbay/issues/293)

## [0.5.0](https://github.com/mdopp/solbay/compare/v0.4.1...v0.5.0) (2026-06-08)


### Features

* **chat:** combine persona+speed dropdown, left-align search, name Zuhause chat ([4c73411](https://github.com/mdopp/solbay/commit/4c734119a32dd5f08ae7ab3cb0910c65b87ec5ac)), closes [#278](https://github.com/mdopp/solbay/issues/278) [#280](https://github.com/mdopp/solbay/issues/280) [#281](https://github.com/mdopp/solbay/issues/281)
* **chat:** declutter header — Thinking toggle + context-fixed selectors ([4778935](https://github.com/mdopp/solbay/commit/47789359332d96d6c59a4e4a0ad93a0bcc24ebee))
* **chat:** declutter header — Thinking toggle + context-fixed selectors ([0b5d330](https://github.com/mdopp/solbay/commit/0b5d33036a13369bc1dfa34bc233a2bfee149990)), closes [#274](https://github.com/mdopp/solbay/issues/274)
* **chat:** household first-turn reply + header dropdown/title + SOUL HA grounding ([fa8a76e](https://github.com/mdopp/solbay/commit/fa8a76e55dd1bcd5a321db5880d13f275fc80c2e))
* **chat:** inline #tag/[@person](https://github.com/person) multitag — mentions backend, autosuggest, tag-cloud, retire Thema picker ([ca02fac](https://github.com/mdopp/solbay/commit/ca02facdd109e6d0a4b4defd4a75bb5ff1c5663b))
* **chat:** mention autosuggest popover + sent-turn highlight (279b) ([637306f](https://github.com/mdopp/solbay/commit/637306f423ace720e9cac696e2375f764b2f2e7e)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **chat:** mentions backend for inline #tag/[@person](https://github.com/person) (279a) ([94bee55](https://github.com/mdopp/solbay/commit/94bee551409e2dc17b5909c370609765517a0d14)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **chat:** responsive tag-cloud + jump-to-message (279c) ([d006c4d](https://github.com/mdopp/solbay/commit/d006c4dfe3d31483ed3fdabb9a6f721f5824fcb4)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **chat:** retire user-facing Thema topic picker (279d) ([98e610d](https://github.com/mdopp/solbay/commit/98e610d544c92b7dbccab8e207f4bff1b026db32)), closes [#279](https://github.com/mdopp/solbay/issues/279)
* **template:** merge hermes/chat/solbay/admin-soul into one solilos service ([6fe0d11](https://github.com/mdopp/solbay/commit/6fe0d114d525c1728b70467ef080b0c2a26575f6))
* **template:** merge hermes/chat/solbay/admin-soul into one solilos service ([dab508e](https://github.com/mdopp/solbay/commit/dab508e014b830adf32019c159b81fe3a8407d26)), closes [#271](https://github.com/mdopp/solbay/issues/271)
* **template:** TTFT prefill trim - drop household admin MCP, ollama anti-eviction, disable kanban ([867ab8e](https://github.com/mdopp/solbay/commit/867ab8ee325b9b3e4b53b2ed7505f5b7459c667c))
* **template:** TTFT trim — drop household servicebay_admin MCP, ollama anti-eviction, disable kanban ([8131fa7](https://github.com/mdopp/solbay/commit/8131fa72fedd4337606367001929fa81f61bbb84)), closes [#268](https://github.com/mdopp/solbay/issues/268)


### Bug Fixes

* **chat:** give first-turn session a unique title to avoid bare-marker collision ([87f5b78](https://github.com/mdopp/solbay/commit/87f5b783e3d37f574fecbffe19f47499b0967204)), closes [#277](https://github.com/mdopp/solbay/issues/277)
* **chat:** unique ephemeral [temp:] title so a 2nd incognito chat can't 400 ([7c479fe](https://github.com/mdopp/solbay/commit/7c479fefe57c7b18f9266fba387fc7cfc0f993ae))
* **chat:** unique ephemeral [temp:] title so a 2nd incognito chat can't 400 ([c403381](https://github.com/mdopp/solbay/commit/c4033817d949a68ebdc58347e14794720082fc6c)), closes [#286](https://github.com/mdopp/solbay/issues/286)
* **template:** land shipped SOUL.md changes on existing installs via a shipped-hash sidecar ([9dcbd79](https://github.com/mdopp/solbay/commit/9dcbd790220d4d1f2f61625c79ddf5eaf1fb6984))
* **template:** land shipped SOUL.md changes on existing installs via a shipped-hash sidecar ([7e0da6d](https://github.com/mdopp/solbay/commit/7e0da6d5d8ed368e2c82e81eb34a8c0d47041a26)), closes [#283](https://github.com/mdopp/solbay/issues/283)
* **template:** SOUL.md grounds device/state questions in live HA tool calls ([1e25a43](https://github.com/mdopp/solbay/commit/1e25a4360cadfc8c78beb491161a5ba09532f03d)), closes [#276](https://github.com/mdopp/solbay/issues/276)

## [0.4.1](https://github.com/mdopp/solbay/compare/v0.4.0...v0.4.1) (2026-06-08)


### Bug Fixes

* **chat,template:** per-turn time grounding, SOUL.md scanner, compaction title collision ([a5dd663](https://github.com/mdopp/solbay/commit/a5dd6631103d0a80201c30567f56ac2cd446734b))
* **chat:** drive pinned household row highlight from selection state ([adabd35](https://github.com/mdopp/solbay/commit/adabd35bda23d9dc308084ec54847f7afa22cc33))
* **chat:** drive pinned household row highlight from selection state ([1d270de](https://github.com/mdopp/solbay/commit/1d270ded2a4ca0125936df8449f3f0c5d996a7be)), closes [#262](https://github.com/mdopp/solbay/issues/262)
* **chat:** give compaction continuation a unique title ([76790dc](https://github.com/mdopp/solbay/commit/76790dce633f250ac1343f68e0283ebdac2f0674)), closes [#267](https://github.com/mdopp/solbay/issues/267)
* **template:** ground Hermes time per-turn and de-trigger SOUL.md scanner ([4db2522](https://github.com/mdopp/solbay/commit/4db25226511de45b7e2e769093226d32f2f91250)), closes [#265](https://github.com/mdopp/solbay/issues/265) [#266](https://github.com/mdopp/solbay/issues/266)

## [0.4.0](https://github.com/mdopp/solbay/compare/v0.3.0...v0.4.0) (2026-06-08)


### Features

* **ci:** add workflow_dispatch to build-images + post-release trigger in release-please ([d02bbd2](https://github.com/mdopp/solbay/commit/d02bbd28f1f3ad82b9f2e2dfaac31266e42847cb))
* **ci:** add workflow_dispatch to build-images + post-release trigger in release-please ([5fff7e8](https://github.com/mdopp/solbay/commit/5fff7e8d0063362d06feafd77fd50773316423b6)), closes [#256](https://github.com/mdopp/solbay/issues/256)


### Bug Fixes

* **chat:** surface tool-turn reply text instead of an empty bubble ([7b16abb](https://github.com/mdopp/solbay/commit/7b16abb1bfe98fef7393b1db26b3cd073c30fcf9))
* **chat:** surface tool-turn reply text instead of an empty bubble ([380ea52](https://github.com/mdopp/solbay/commit/380ea52350e39b57daeda4c432200d4a00e4ef79)), closes [#258](https://github.com/mdopp/solbay/issues/258)

## [0.3.0](https://github.com/mdopp/solbay/compare/v0.2.0...v0.3.0) (2026-06-08)


### Features

* **chat:** adaptive + selectable reasoning routing with rendered thinking ([e98a825](https://github.com/mdopp/solbay/commit/e98a825fd8ea1a360a000f7268d0960908aeebec)), closes [#222](https://github.com/mdopp/solbay/issues/222) [#224](https://github.com/mdopp/solbay/issues/224)
* **chat:** adaptive context window from live Ollama model ([41505d4](https://github.com/mdopp/solbay/commit/41505d48ecd8fd9f45e01840a94c5d95b879bea5))
* **chat:** assign chat topics — session_topics table + picker + chip ([119c131](https://github.com/mdopp/solbay/commit/119c1315469f5017f147428628a5f2c7f55f881d)), closes [#241](https://github.com/mdopp/solbay/issues/241)
* **chat:** auto-tag ingestion with #topic/&lt;slug&gt; from the active topic ([1f48d8b](https://github.com/mdopp/solbay/commit/1f48d8b2d2f02c44c341bab1dcdae9e7abbad383)), closes [#243](https://github.com/mdopp/solbay/issues/243)
* **chat:** bind a topic's default model and persona at session create ([44e4f6a](https://github.com/mdopp/solbay/commit/44e4f6ae7390f460b0278f5705c45cd876310cd9)), closes [#242](https://github.com/mdopp/solbay/issues/242)
* **chat:** chat compaction — extract durable learnings, then compact [#210](https://github.com/mdopp/solbay/issues/210) ([3e872b5](https://github.com/mdopp/solbay/commit/3e872b5bee3800963ee25596070355b0c5299be0))
* **chat:** derive compaction context window from the live Ollama model ([dde868f](https://github.com/mdopp/solbay/commit/dde868f34de997a47d286b41c1e63855381af00e)), closes [#235](https://github.com/mdopp/solbay/issues/235)
* **chat:** embed cosmetics + CSP frame-ancestors ([89a7b6f](https://github.com/mdopp/solbay/commit/89a7b6fa80e0f818a5f4c1520b1d6e5cf8aa9b8c))
* **chat:** embed cosmetics, accent theme override, focused layout ([8cdf9d4](https://github.com/mdopp/solbay/commit/8cdf9d4c0daae213d1e3ef34bd442d9e365b3c7b)), closes [#227](https://github.com/mdopp/solbay/issues/227)
* **chat:** mobile rail/footer/composer polish cluster ([9d06b1c](https://github.com/mdopp/solbay/commit/9d06b1c7b6b23f924d73bacc5f7079c4b6b2e606))
* **chat:** mobile rail/footer/composer polish cluster ([22a64f6](https://github.com/mdopp/solbay/commit/22a64f61c24ac798a901e3aa0606844c968d08b4)), closes [#217](https://github.com/mdopp/solbay/issues/217) [#216](https://github.com/mdopp/solbay/issues/216) [#213](https://github.com/mdopp/solbay/issues/213) [#212](https://github.com/mdopp/solbay/issues/212) [#211](https://github.com/mdopp/solbay/issues/211)
* **chat:** per-turn latency trace waterfall under each reply ([397be99](https://github.com/mdopp/solbay/commit/397be99929ad83604df7731fd4e74f3ac871de61)), closes [#225](https://github.com/mdopp/solbay/issues/225)
* **chat:** server-side ServiceBay-maintenance persona lock ([ec8b4a3](https://github.com/mdopp/solbay/commit/ec8b4a3ae62a519e7f6888f9f92db4dc62121add))
* **chat:** server-side ServiceBay-maintenance persona lock ([14e5c08](https://github.com/mdopp/solbay/commit/14e5c0874f0bc497d2df0affa00497a74850698b)), closes [#229](https://github.com/mdopp/solbay/issues/229)
* **chat:** temporary/incognito chat — ephemeral by default with extract-to-topic escape hatch ([b88019c](https://github.com/mdopp/solbay/commit/b88019ce21a46f1f7b8a72972aa1411aa0052ba0)), closes [#246](https://github.com/mdopp/solbay/issues/246)
* **chat:** topic-filtered retrieval — notes-search by topic + topic dashboard ([3c5c9e3](https://github.com/mdopp/solbay/commit/3c5c9e3f2030c3041ce969558c7fdc90ec30b2cb)), closes [#244](https://github.com/mdopp/solbay/issues/244)
* **latency:** e2b/12b model routing, 128k window, keep-alive, conservative toolset trim ([f19bad9](https://github.com/mdopp/solbay/commit/f19bad96dc3fd185d5a71a26fe4f25a815b11629))
* **skill:** admin operator soul skill pack ([b787e69](https://github.com/mdopp/solbay/commit/b787e69499bb2dc891a00b54017192346efdb063))
* **skill:** admin operator soul skill pack ([ec025b8](https://github.com/mdopp/solbay/commit/ec025b8c3a86e8cabb30b5000e1ee65174d6e340)), closes [#176](https://github.com/mdopp/solbay/issues/176)
* **skill:** topic suggestion — propose a topic for a recurring theme, create on confirm ([59eb9e7](https://github.com/mdopp/solbay/commit/59eb9e75c2e0cc19363ac039b3172446e5e55d05)), closes [#245](https://github.com/mdopp/solbay/issues/245)
* **solbay:** full-bleed chat composer bar ([f7cf2bb](https://github.com/mdopp/solbay/commit/f7cf2bbbb463b4e9a627a5ade7cddf515ee1d0db))
* **solbay:** full-bleed chat composer bar ([312a043](https://github.com/mdopp/solbay/commit/312a043ae1c42d99d26aaf0ee194acbfa0e73399)), closes [#201](https://github.com/mdopp/solbay/issues/201)
* **solilos-chat:** add pinned household chat pre-bound to household topic ([49a16f4](https://github.com/mdopp/solbay/commit/49a16f4c3e15ae05c6484101ae1f3a28d332d730)), closes [#237](https://github.com/mdopp/solbay/issues/237)
* **template:** adaptive e2b/12b model routing, 128k window, keep-alive, wider toolset trim ([b9b8e00](https://github.com/mdopp/solbay/commit/b9b8e00c4c95a9d17e69adc098d102d9644f1123))
* **template:** add FRAME_ANCESTORS CSP frame-ancestors for the chat embed ([8e2fa57](https://github.com/mdopp/solbay/commit/8e2fa575cfc7db7ec1c2d14643d13605c60b7f43)), closes [#228](https://github.com/mdopp/solbay/issues/228)
* **template:** auto-install HA Jellyfin integration via config-flow API ([8977b66](https://github.com/mdopp/solbay/commit/8977b667423781c91f1a575e36bb8817c08b550a))
* **template:** auto-install HA Jellyfin integration via config-flow API ([0f0fbb4](https://github.com/mdopp/solbay/commit/0f0fbb45148937297cc9b64a114656514c38f407)), closes [#195](https://github.com/mdopp/solbay/issues/195)
* **template:** tune ollama gemma4:12b efficiency and add dedicated embed model ([e014a91](https://github.com/mdopp/solbay/commit/e014a91047662191b0e027fcd678c62a6d2c487b)), closes [#214](https://github.com/mdopp/solbay/issues/214)
* **topics:** Topics v1 epic ([#239](https://github.com/mdopp/solbay/issues/239)) + temporary chat ([c815245](https://github.com/mdopp/solbay/commit/c815245ecdba343b71aa879164d0d987f01d9ce2))


### Bug Fixes

* **chat:** render reasoning from Hermes reasoning_content, not a thinking tag ([9a81914](https://github.com/mdopp/solbay/commit/9a819148f79e933f95ad7ca00123ad4d7af859d8)), closes [#231](https://github.com/mdopp/solbay/issues/231)
* **chat:** show the Solilos release version in the sidebar badge ([fef817e](https://github.com/mdopp/solbay/commit/fef817e96ec87d53d687375e33e4b01684fd451a)), closes [#223](https://github.com/mdopp/solbay/issues/223)
* **ci:** inject git-describe as SOLILOS_VERSION for the chat badge ([af6efdd](https://github.com/mdopp/solbay/commit/af6efdd9ac3be1eb7bdd8b51e8de49b20a883d03)), closes [#248](https://github.com/mdopp/solbay/issues/248)
* **solbay:** single rail divider and untiled in-app Sol mark ([da081cd](https://github.com/mdopp/solbay/commit/da081cd82471fb8b6e709ed6a351c0d5cf2d6cea)), closes [#219](https://github.com/mdopp/solbay/issues/219) [#220](https://github.com/mdopp/solbay/issues/220)
* **solilos-chat:** skip trivial device-control turns in compaction extract ([6de38ff](https://github.com/mdopp/solbay/commit/6de38ff5c11bc4f9b9b79f02f26461231833a472)), closes [#250](https://github.com/mdopp/solbay/issues/250)
* **template:** disable unused Hermes toolsets to shrink cold-cache prefill ([9caadcd](https://github.com/mdopp/solbay/commit/9caadcd6ef050ae6ddaf160c72274b16e226ec5c)), closes [#230](https://github.com/mdopp/solbay/issues/230)
* **template:** mount solilos.db and notes vault into solilos-chat pod ([2367ed9](https://github.com/mdopp/solbay/commit/2367ed95b50ab74d1b71ce7dacd81fc06b4c155a))
* **template:** mount solilos.db and notes vault into solilos-chat pod ([2c8b680](https://github.com/mdopp/solbay/commit/2c8b680d002772f611b4dd0207ab25bc48866d67))
* **template:** re-enable household toolsets, keep only clearly-unused disabled ([a46640f](https://github.com/mdopp/solbay/commit/a46640f9cb3c592099c7c5044378ffc65ba1b0ec))

## Changelog

This changelog is maintained by [release-please](https://github.com/googleapis/release-please)
from the conventional commits on `main`. Release history before this file is in
the [GitHub releases](https://github.com/mdopp/solbay/releases).
