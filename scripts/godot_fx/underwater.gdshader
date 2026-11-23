shader_type spatial;
render_mode unshaded, depth_test_disabled, cull_disabled, shadows_disabled, fog_disabled;
// Effet sous l'eau : absorption/brouillard teinte, caustiques sur les surfaces, rayons de lumiere,
// planctons, dithering. Equivalent Godot de l'effet "DIVE" de Rafael's Shader Pack.
// Quad plein ecran attache a la camera par underwater.gd quand elle passe sous un plan d'eau.

uniform sampler2D screen_tex : hint_screen_texture, filter_linear_mipmap, repeat_disable;
uniform sampler2D depth_tex : hint_depth_texture, filter_nearest, repeat_disable;
uniform sampler2D noise_tex : repeat_enable, filter_linear_mipmap, hint_default_white;

uniform vec3 water_color : source_color = vec3(0.080, 0.159, 0.171);   // valeurs par defaut de DIVE
uniform float absorption = 0.09;
uniform float surface_y = 0.0;
uniform bool caustics_enabled = true;
uniform bool rays_enabled = true;
uniform bool plankton_enabled = true;
uniform float dithering = 1.0;

float hash12(vec2 p) {
	vec3 p3 = fract(vec3(p.xyx) * 0.1031);
	p3 += dot(p3, p3.yzx + 33.33);
	return fract((p3.x + p3.y) * p3.z);
}

float caustic(vec2 p) {
	float a = texture(noise_tex, p * 0.11 + vec2(TIME * 0.020, TIME * 0.013)).r;
	float b = texture(noise_tex, p * 0.17 - vec2(TIME * 0.016, -TIME * 0.021)).r;
	float c = 1.0 - abs(a + b - 1.0) * 2.4;             // reseau de lignes lumineuses
	return pow(clamp(c, 0.0, 1.0), 3.0);
}

void vertex() {
	POSITION = vec4(VERTEX.xy, 1.0, 1.0);          // quad plein ecran
}

void fragment() {
	vec3 scene = textureLod(screen_tex, SCREEN_UV, 0.0).rgb;
	float d = texture(depth_tex, SCREEN_UV).x;
	vec3 ndc = vec3(SCREEN_UV * 2.0 - 1.0, d);
	vec4 view = INV_PROJECTION_MATRIX * vec4(ndc, 1.0);
	view.xyz /= view.w;
	float dist = length(view.xyz);
	bool is_sky = d <= 0.00001;
	if (is_sky) dist = 400.0;
	vec3 world = (INV_VIEW_MATRIX * vec4(view.xyz, 1.0)).xyz;
	vec3 cam_pos = INV_VIEW_MATRIX[3].xyz;
	float cam_depth = max(surface_y - cam_pos.y, 0.0);

	// lumiere qui descend depuis la surface : plus sombre en profondeur
	float light = exp(-cam_depth * 0.035);
	vec3 col = scene;

	// caustiques sur les surfaces eclairees (sous la surface seulement)
	if (caustics_enabled && !is_sky) {
		float depth_below = max(surface_y - world.y, 0.0);
		float k = caustic(world.xz) * exp(-depth_below * 0.12) * step(world.y, surface_y);
		col += vec3(0.55, 0.85, 0.9) * k * 0.55;
	}

	// brouillard d'eau (absorption exponentielle)
	float fog = 1.0 - exp(-dist * absorption);
	vec3 fog_col = water_color * (0.35 + 0.65 * light);
	col = mix(col * vec3(0.55, 0.85, 0.95), fog_col, fog);

	// rayons de lumiere : faisceaux larges et oscillants, plus visibles vers le haut de l'ecran
	if (rays_enabled) {
		vec2 uv = vec2(SCREEN_UV.x * 1.6 + sin(TIME * 0.17 + SCREEN_UV.y * 2.0) * 0.08, SCREEN_UV.y * 0.25 - TIME * 0.012);
		float r = texture(noise_tex, uv).r * texture(noise_tex, uv * 1.9 + vec2(0.31, TIME * 0.007)).r;
		float rays = pow(clamp(r * 2.0, 0.0, 1.0), 2.5) * smoothstep(0.0, 1.0, 1.0 - SCREEN_UV.y);
		col += vec3(0.30, 0.55, 0.55) * rays * 0.22 * light;
	}

	// plancton : petits points lumineux qui flottent
	if (plankton_enabled && !is_sky) {
		vec2 g = world.xz * 2.5 + vec2(sin(TIME * 0.3 + world.y), cos(TIME * 0.25)) * 0.5;
		float sp = hash12(floor(g * 6.0 + world.y * 2.0));
		col += vec3(0.5, 1.0, 0.8) * step(0.9985, sp) * exp(-dist * 0.15) * 0.8;
	}

	col += (hash12(FRAGCOORD.xy + TIME) - 0.5) / 255.0 * dithering * 2.0;
	ALBEDO = col;
}
