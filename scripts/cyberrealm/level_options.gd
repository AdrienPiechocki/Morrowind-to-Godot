extends Node
## Options du niveau dans le menu pause (bouton « Level » du menu principal).
##
## Modifiables par l'HÔTE (ou en solo) ; les autres joueurs voient les valeurs
## en lecture seule. Chaque changement est appliqué localement puis diffusé aux
## clients (RPC "authority" : seul l'hôte peut en émettre). L'état vit dans les
## nœuds du niveau (DayNight.real_time / time_of_day, Weather.raining), donc un
## joueur qui rejoint le reçoit déjà avec le niveau baké.
##
## Le menu pause n'a pas de point d'extension : on s'y greffe au runtime
## (bouton ajouté à la page principale à chaque reconstruction de celle-ci).
## Tout est retiré à la sortie de l'arbre (changement de niveau).

const BTN_META := "level_options_btn"
const BROADCAST_DELAY := 0.25     # regroupe les rafales (curseur) en un seul envoi

var _pm: Node = null              # PauseMenu du joueur local
var _btn: Button = null
var _dirty := -1.0
var _time_mode: OptionButton = null
var _hour_slider: HSlider = null
var _hour_label: Label = null
var _rain_btn: CheckButton = null
var _fog_slider: HSlider = null
var _fog_label: Label = null
var _glare_slider: HSlider = null
var _glare_label: Label = null
var _cloud_slider: HSlider = null
var _cloud_label: Label = null
var _info: Label = null

func _process(delta: float) -> void:
	if _pm == null:
		_try_attach()
	if _dirty >= 0.0:
		_dirty -= delta
		if _dirty < 0.0:
			_broadcast()

func _exit_tree() -> void:
	if _pm != null and is_instance_valid(_pm):
		var c := _pm.get("container") as Container
		if c != null and c.child_order_changed.is_connected(_on_container_changed):
			c.child_order_changed.disconnect(_on_container_changed)
		if _btn != null and is_instance_valid(_btn):
			_btn.queue_free()
		# Si la page « Level » est affichée, revenir au menu principal.
		if _pm.get("_current_view") == "level_options" and _pm.visible:
			_pm.call("_show_main")

func _try_attach() -> void:
	var player := get_parent().find_child("Player", true, false)
	var pm := player.get_node_or_null("PauseMenuLayer/PauseMenu") if player != null else null
	if pm == null or pm.get("container") == null:
		return
	_pm = pm
	var c := pm.get("container") as Container
	c.child_order_changed.connect(_on_container_changed)
	_on_container_changed()

func _on_container_changed() -> void:
	_inject.call_deferred()

# ── Greffe sur le menu principal ─────────────────────────────────────

func _inject() -> void:
	if _pm == null or not is_instance_valid(_pm) or _pm.get("_current_view") != "main":
		return
	var c := _pm.get("container") as Container
	for ch in c.get_children():
		if ch.has_meta(BTN_META) and not ch.is_queued_for_deletion():
			return
	_btn = _pm.call("_make_btn", "Level") as Button
	_btn.set_meta(BTN_META, true)
	_btn.pressed.connect(_show_page)
	c.add_child(_btn)
	# Avant le ressort (premier enfant non-bouton qui prend la hauteur libre).
	var idx := c.get_child_count() - 1
	for i in c.get_child_count() - 1:
		var ch := c.get_child(i)
		# Les enfants de la page précédente (queue_free en attente) sont encore là.
		if ch.is_queued_for_deletion():
			continue
		if not (ch is BaseButton) and ch is Control \
				and (ch as Control).size_flags_vertical & Control.SIZE_EXPAND:
			idx = i
			break
	c.move_child(_btn, idx)

# ── Page « Level » ───────────────────────────────────────────────────

func _is_host() -> bool:
	return get_tree().get_multiplayer().is_server()

func _nodes() -> Dictionary:
	var lvl := get_parent()
	return {"dn": lvl.get_node_or_null("DayNight"), "weather": lvl.get_node_or_null("Weather")}

func _show_page() -> void:
	_pm.call("_clear")
	_pm.set("_current_view", "level_options")
	var c := _pm.get("container") as Container
	var host := _is_host()
	var n := _nodes()
	var dn: Node = n["dn"]
	var weather: Node = n["weather"]
	c.add_child(_pm.call("_make_title", "LEVEL OPTIONS"))
	if not host:
		_info = Label.new()
		_info.text = "Only the host can change the level options."
		_info.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		_info.add_theme_font_size_override("font_size", 14)
		c.add_child(_info)

	# ── Heure ──
	c.add_child(_pm.call("_make_row_label", "Time of day"))
	_time_mode = OptionButton.new()
	_time_mode.add_item("Real time (local clock)", 0)
	_time_mode.add_item("Fixed hour", 1)
	_time_mode.custom_minimum_size = Vector2(0, 36)
	_time_mode.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_time_mode.add_theme_font_size_override("font_size", 14)
	_hour_label = Label.new()
	_hour_label.add_theme_font_size_override("font_size", 14)
	_hour_slider = HSlider.new()
	_hour_slider.min_value = 0.0
	_hour_slider.max_value = 23.75
	_hour_slider.step = 0.25
	_hour_slider.custom_minimum_size = Vector2(0, 28)
	_hour_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	if dn != null:
		_time_mode.selected = 0 if dn.get("real_time") else 1
		_hour_slider.value = snappedf(float(dn.get("time_of_day")), 0.25)
	_update_hour_label()
	c.add_child(_time_mode)
	c.add_child(_hour_label)
	c.add_child(_hour_slider)

	# ── Brume ──
	c.add_child(_pm.call("_make_row_label", "Fog thickness"))
	_fog_label = Label.new()
	_fog_label.add_theme_font_size_override("font_size", 14)
	_fog_slider = HSlider.new()
	_fog_slider.min_value = 0.0
	_fog_slider.max_value = 5.0
	_fog_slider.step = 0.1
	_fog_slider.custom_minimum_size = Vector2(0, 28)
	_fog_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_fog_slider.value = float(dn.get("fog_thickness")) if dn != null and dn.get("fog_thickness") != null else 1.0
	_update_fog_label()
	c.add_child(_fog_label)
	c.add_child(_fog_slider)

	# ── Nuages ──
	c.add_child(_pm.call("_make_row_label", "Cloud density"))
	_cloud_label = Label.new()
	_cloud_label.add_theme_font_size_override("font_size", 14)
	_cloud_slider = HSlider.new()
	_cloud_slider.min_value = 0.0
	_cloud_slider.max_value = 1.0
	_cloud_slider.step = 0.05
	_cloud_slider.custom_minimum_size = Vector2(0, 28)
	_cloud_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_cloud_slider.value = float(dn.get("cloud_density")) if dn != null and dn.get("cloud_density") != null else 0.43
	_update_cloud_label()
	c.add_child(_cloud_label)
	c.add_child(_cloud_slider)

	# ── Halo du soleil ──
	c.add_child(_pm.call("_make_row_label", "Sun glare"))
	_glare_label = Label.new()
	_glare_label.add_theme_font_size_override("font_size", 14)
	_glare_slider = HSlider.new()
	_glare_slider.min_value = 0.0
	_glare_slider.max_value = 3.0
	_glare_slider.step = 0.05
	_glare_slider.custom_minimum_size = Vector2(0, 28)
	_glare_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var glare := get_parent().get_node_or_null("SunGlare")
	_glare_slider.value = float(glare.get("glare_intensity")) if glare != null else 1.0
	_update_glare_label()
	c.add_child(_glare_label)
	c.add_child(_glare_slider)

	# ── Pluie ──
	_rain_btn = CheckButton.new()
	_rain_btn.text = "Rain"
	_rain_btn.custom_minimum_size = Vector2(0, 36)
	_rain_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_rain_btn.add_theme_font_size_override("font_size", 14)
	_rain_btn.button_pressed = weather != null and bool(weather.get("raining"))
	c.add_child(_rain_btn)

	_refresh_enabled()
	if host:
		_fog_slider.value_changed.connect(_on_changed)
		_cloud_slider.value_changed.connect(_on_changed)
		_glare_slider.value_changed.connect(_on_changed)
		_time_mode.item_selected.connect(_on_changed)
		_hour_slider.value_changed.connect(_on_changed)
		_rain_btn.toggled.connect(_on_changed)
	c.add_child(_pm.call("_make_spacer"))
	c.add_child(_pm.call("_make_back_btn"))

func _refresh_enabled() -> void:
	var host := _is_host()
	_time_mode.disabled = not host
	_rain_btn.disabled = not host
	_glare_slider.editable = host
	_glare_slider.modulate.a = 1.0 if host else 0.5
	_cloud_slider.editable = host
	_cloud_slider.modulate.a = 1.0 if host else 0.5
	# Brume : automatique (selon l'heure) en heure réelle ; curseur seulement en heure fixe.
	_fog_slider.editable = host and _time_mode.selected == 1
	_fog_slider.modulate.a = 1.0 if _fog_slider.editable else 0.5
	_hour_slider.editable = host and _time_mode.selected == 1
	_hour_slider.modulate.a = 1.0 if _hour_slider.editable else 0.5

func _update_hour_label() -> void:
	var h := _hour_slider.value
	if _time_mode.selected == 0:
		var d := Time.get_datetime_dict_from_system()
		_hour_label.text = "Now: %02d:%02d" % [d.hour, d.minute]
	else:
		_hour_label.text = "Hour: %02d:%02d" % [int(h), int(round((h - floorf(h)) * 60.0))]

func _update_fog_label() -> void:
	if _time_mode.selected == 0:
		_fog_label.text = "Fog: automatic (follows the time of day)"
	else:
		_fog_label.text = "Fog: x%.1f%s" % [_fog_slider.value, " (off)" if _fog_slider.value <= 0.0 else ""]

func _update_glare_label() -> void:
	_glare_label.text = "Glare: x%.2f%s" % [_glare_slider.value, " (off)" if _glare_slider.value <= 0.0 else ""]

func _update_cloud_label() -> void:
	_cloud_label.text = "Clouds: %d%%" % int(round(_cloud_slider.value * 100.0))

func _on_changed(_v = null) -> void:
	_update_glare_label()
	_update_cloud_label()
	_refresh_enabled()
	_update_hour_label()
	_update_fog_label()
	_apply(_state_from_ui())
	_dirty = BROADCAST_DELAY

func _state_from_ui() -> Dictionary:
	return {"fixed": _time_mode.selected == 1, "hour": _hour_slider.value, "rain": _rain_btn.button_pressed,
		"fog": _fog_slider.value,
		"cloud": _cloud_slider.value, "glare": _glare_slider.value}

# ── Application + réseau ─────────────────────────────────────────────

func _apply(state: Dictionary) -> void:
	var n := _nodes()
	var dn: Node = n["dn"]
	var weather: Node = n["weather"]
	if dn != null:
		var fixed := bool(state.get("fixed", false))
		dn.set("real_time", not fixed)
		dn.set("day_length", 0.0)
		if fixed:
			dn.set("time_of_day", clampf(float(state.get("hour", 12.0)), 0.0, 24.0))
	if dn != null and state.has("fog"):
		dn.set("fog_thickness", clampf(float(state["fog"]), 0.0, 5.0))
	var glare_node := get_parent().get_node_or_null("SunGlare")
	if glare_node != null and state.has("glare"):
		glare_node.set("glare_intensity", clampf(float(state["glare"]), 0.0, 3.0))
	if dn != null and state.has("cloud"):
		dn.set("cloud_density", clampf(float(state["cloud"]), 0.0, 1.0))
	if weather != null:
		weather.set("raining", bool(state.get("rain", false)))

func _broadcast() -> void:
	_dirty = -1.0
	if not _is_host() or get_tree().get_multiplayer().get_peers().is_empty():
		return
	_net_apply.rpc(_state_from_ui())

@rpc("authority", "call_remote", "reliable")
func _net_apply(state: Dictionary) -> void:
	# Garde-fous : un hôte malveillant ne peut envoyer que ces champs.
	var h = state.get("hour", 12.0)
	var f = state.get("fog", 1.0)
	var cl = state.get("cloud", 0.43)
	var gl = state.get("glare", 1.0)
	_apply({"fixed": state.get("fixed", false) == true,
		"hour": float(h) if typeof(h) == TYPE_FLOAT or typeof(h) == TYPE_INT else 12.0,
		"rain": state.get("rain", false) == true,
		"fog": float(f) if typeof(f) == TYPE_FLOAT or typeof(f) == TYPE_INT else 1.0,
		"cloud": float(cl) if typeof(cl) == TYPE_FLOAT or typeof(cl) == TYPE_INT else 0.43,
		"glare": float(gl) if typeof(gl) == TYPE_FLOAT or typeof(gl) == TYPE_INT else 1.0})
