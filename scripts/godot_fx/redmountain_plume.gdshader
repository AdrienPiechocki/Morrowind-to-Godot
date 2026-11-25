shader_type spatial;
render_mode unshaded, cull_disabled, depth_test_disabled, depth_draw_never, shadows_disabled, fog_disabled, blend_premul_alpha;
// Panache de fumee du Mont Ecarlate : raymarching volumetrique dans une boite englobante.
// Colonne qui s'evase et se penche sous le vent, bruit FBM deforme, eclairage solaire avec ombrage
// propre, diffusion vers l'avant, lueur de lave a la base, brume atmospherique avec la distance.
// Code original (equivalent Godot de l'effet RedMountainSmoke).

uniform vec3 box_min;
uniform vec3 box_max;
uniform vec3 origin;                    // base du panache (cratere), repere monde Godot (m)
uniform float height = 860.0;
uniform float radius_base = 2.0;
uniform float radius_top = 211.0;
uniform float bend = 650.0;
uniform vec2 wind_dir = vec2(-0.916, 0.401);   // vers ou penche le sommet (x, z monde)
uniform float coverage : hint_range(0.0, 1.0) = 0.66;
uniform float edge_softness : hint_range(0.05, 1.0) = 0.42;
uniform float top_dilute : hint_range(0.0, 1.0) = 0.15;
uniform float density = 1.0;
uniform vec3 tint : source_color = vec3(0.82, 0.80, 0.70);
uniform float noise_scale = 0.012;      // frequence de base (1/m)
uniform float warp = 0.45;
uniform float detail = 0.55;
uniform float rise_speed = 10.0;        // m/s
uniform float extinction = 0.035;       // opacite par metre a densite 1
uniform int steps : hint_range(8, 128) = 56;
uniform int light_steps : hint_range(0, 8) = 3;
uniform float sun_strength = 3.0;
uniform float ambient_strength = 0.25;
uniform float forward_strength = 0.68;
uniform float lava_glow = 2.0;
uniform float lava_radius = 60.0;
uniform vec3 sun_dir = vec3(0.3, 0.8, 0.5);     // direction VERS le soleil
uniform vec3 sun_color = vec3(1.0, 0.95, 0.85);
uniform vec3 ambient_color = vec3(0.45, 0.55, 0.75);
uniform vec3 fog_color = vec3(0.58, 0.72, 0.88);
uniform float fade_start = 1500.0;           // m : opacite pleine en dessous
uniform float fade_end = 7000.0;             // m : invisible au-dela
uniform float haze_density = 0.00035;           // 1/m
uniform float haze_floor : hint_range(0.0, 1.0) = 0.5;    // perspective atmospherique minimale : meme voile que la montagne
uniform vec3 aerial_tint : source_color = vec3(0.243, 0.380, 0.596);   // teinte lointaine de l'impostor
uniform float day_light = 1.0;           // 0 nuit .. 1 jour (voile atmospherique)
uniform float base_sink = 30.0;                 // m : la colonne nait sous le bord du cratere (cachee par l'arete avant)
uniform bool mt_enabled = false;                // impostor de la montagne present
uniform vec3 mt_center = vec3(0.0);             // centre du plan de l'impostor (monde)
uniform float mt_size = 896.87;                 // cote du plan de l'impostor (m)
uniform float mt_front = 400.0;                // marge (m) devant le plan de la montagne ou la fumee reste masquee
uniform sampler2D mt_atlas0 : filter_linear_mipmap, repeat_disable;
uniform sampler2D mt_atlas1 : filter_linear_mipmap, repeat_disable;
uniform sampler2D mt_atlas2 : filter_linear_mipmap, repeat_disable;
uniform sampler2D mt_atlas3 : filter_linear_mipmap, repeat_disable;
uniform sampler2D blue_noise : filter_nearest, repeat_enable, hint_default_white;
uniform sampler2D depth_tex : hint_depth_texture, filter_nearest, repeat_disable;

float hash13(vec3 p3) {
	p3 = fract(p3 * 0.1031);
	p3 += dot(p3, p3.zyx + 31.32);
	return fract((p3.x + p3.y) * p3.z);
}

float vnoise(vec3 p) {
	vec3 i = floor(p);
	vec3 f = fract(p);
	f = f * f * (3.0 - 2.0 * f);
	float n000 = hash13(i);
	float n100 = hash13(i + vec3(1, 0, 0));
	float n010 = hash13(i + vec3(0, 1, 0));
	float n110 = hash13(i + vec3(1, 1, 0));
	float n001 = hash13(i + vec3(0, 0, 1));
	float n101 = hash13(i + vec3(1, 0, 1));
	float n011 = hash13(i + vec3(0, 1, 1));
	float n111 = hash13(i + vec3(1, 1, 1));
	return mix(mix(mix(n000, n100, f.x), mix(n010, n110, f.x), f.y),
			mix(mix(n001, n101, f.x), mix(n011, n111, f.x), f.y), f.z);
}

float fbm(vec3 p, int oct) {
	float a = 0.5, s = 0.0;
	for (int i = 0; i < 4; i++) {
		if (i >= oct) break;
		s += a * vnoise(p);
		p = p * 2.07 + vec3(1.7, 9.2, 3.1);
		a *= 0.5;
	}
	return s;
}

// densite de fumee au point monde p (0 hors colonne)
float smoke(vec3 p, int oct) {
	vec3 rel = p - (origin - vec3(0.0, base_sink, 0.0));
	float h = rel.y;
	float total = height + base_sink;
	if (h < 0.0 || h > total) return 0.0;
	float t = h / total;
	vec2 axis = wind_dir * bend * t * t;                    // la colonne se penche vers le haut
	float r = mix(radius_base, radius_top, pow(t, 0.55));
	float d = length(rel.xz - axis) / r;
	float shape = clamp((1.15 - d) / edge_softness, 0.0, 1.0);
	if (shape <= 0.0) return 0.0;
	vec3 q = (p - vec3(0.0, TIME * rise_speed, 0.0)) * noise_scale;
	float w = fbm(q * 0.5 + 7.3, 2);
	float n = fbm(q + (w - 0.5) * warp * 2.0, oct);
	n += detail * (fbm(q * 3.3 + 2.1, 2) - 0.5) * 0.6;
	float thr = 1.0 - coverage;
	float dens = clamp((n - thr * 0.55) / 0.42, 0.0, 1.0);
	return dens * shape * density * (1.0 - top_dilute * t);
}

float hg(float c, float g) {
	float g2 = g * g;
	return (1.0 - g2) / (4.0 * 3.14159265 * pow(1.0 + g2 - 2.0 * g * c, 1.5));
}

float mt_sprite(int angle, vec2 suv) {
	int sp = angle % 16;
	vec2 cell = vec2(float(sp % 4), float(sp / 4));
	vec2 uv = (cell + suv) / 4.0;
	int atl = angle / 16;
	if (atl == 0) return textureLod(mt_atlas0, uv, 1.5).r;
	if (atl == 1) return textureLod(mt_atlas1, uv, 1.5).r;
	if (atl == 2) return textureLod(mt_atlas2, uv, 1.5).r;
	return textureLod(mt_atlas3, uv, 1.5).r;
}

// Couverture (0..1) de la silhouette de la montagne sur ce rayon ; t_plane = distance du plan de l'impostor.
float mountain_cover(vec3 cam, vec3 rd, out float t_plane) {
	t_plane = 1e9;
	if (!mt_enabled) return 0.0;
	vec3 to_cam = cam - mt_center;
	to_cam.y = 0.0;
	vec3 n = normalize(to_cam + vec3(1e-6, 0.0, 0.0));
	vec3 right = normalize(cross(vec3(0.0, 1.0, 0.0), n));
	float denom = dot(rd, n);
	if (denom > -1e-4) return 0.0;
	t_plane = dot(mt_center - cam, n) / denom;
	vec3 h = cam + rd * t_plane - mt_center;
	vec2 quv = vec2(dot(h, right), h.y) / mt_size + 0.5;
	if (quv.x < 0.0 || quv.x > 1.0 || quv.y < 0.0 || quv.y > 1.0) return 0.0;
	float ang = atan(n.x, -n.z);
	float f = mod(ang / (6.28318530718 / 64.0), 64.0);
	int k = int(f);
	int k2 = (k + 1) % 64;
	float w = clamp((fract(f) - 0.5) / 0.1 + 0.5, 0.0, 1.0);
	vec2 suv = vec2(quv.x, 1.0 - quv.y);
	float m = mix(mt_sprite(k, suv), mt_sprite(k2, suv), w);
	return smoothstep(0.005, 0.03, m);
}

void vertex() {
	POSITION = vec4(VERTEX.x, VERTEX.y, 1.0, 1.0);   // quad plein ecran (QuadMesh 2x2)
}

void fragment() {
	vec3 cam = (INV_VIEW_MATRIX * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
	vec4 vp = INV_PROJECTION_MATRIX * vec4(SCREEN_UV * 2.0 - 1.0, 0.5, 1.0);
	vec3 rd = normalize((INV_VIEW_MATRIX * vec4(vp.xyz / vp.w, 0.0)).xyz);

	// intersection rayon / boite
	vec3 inv = 1.0 / (rd + vec3(1e-7));
	vec3 ta = (box_min - cam) * inv;
	vec3 tb = (box_max - cam) * inv;
	vec3 tmin = min(ta, tb);
	vec3 tmax = max(ta, tb);
	float t0 = max(max(tmin.x, tmin.y), max(tmin.z, 0.0));
	float t1 = min(min(tmax.x, tmax.y), tmax.z);

	// occlusion par la geometrie de la scene
	float d = texture(depth_tex, SCREEN_UV).x;
	if (d > 0.00001) {
		vec3 ndc = vec3(SCREEN_UV * 2.0 - 1.0, d);
		vec4 v = INV_PROJECTION_MATRIX * vec4(ndc, 1.0);
		v.xyz /= v.w;
		t1 = min(t1, length(v.xyz));
	}
	if (t1 <= t0) discard;

	float fade = pow(1.0 - smoothstep(fade_start, fade_end, length(origin - cam)), 1.5);
	if (fade <= 0.002) discard;

	float dt = (t1 - t0) / float(steps);
	vec2 bn_uv = (FRAGCOORD.xy + floor(TIME * 60.0) * vec2(17.0, 31.0)) / vec2(textureSize(blue_noise, 0));
	float jitter = texture(blue_noise, bn_uv).r;
	float cosv = dot(rd, sun_dir);
	float phase = mix(1.0, hg(cosv, 0.55) * 4.0 * 3.14159265 * 0.18, forward_strength);

	float t_plane;
	float mt_cover = mountain_cover(cam, rd, t_plane);
	vec3 col = vec3(0.0);
	float T = 1.0;
	for (int i = 0; i < 128; i++) {
		if (i >= steps) break;
		float t = t0 + (float(i) + jitter) * dt;
		vec3 p = cam + rd * t;
		float dens = smoke(p, 4);
		if (dens > 0.004) {
			float tau = 0.0;
			for (int j = 0; j < 8; j++) {
				if (j >= light_steps) break;
				float ls = 35.0 * (float(j) + 1.0);
				tau += smoke(p + sun_dir * ls, 2) * ls * 0.4;
			}
			float shadow = exp(-tau * extinction * 1.5);
			vec3 light = sun_color * sun_strength * shadow * phase + ambient_color * ambient_strength;
			float h = p.y - origin.y;
			float lava = lava_glow * exp(-max(h, 0.0) / lava_radius) * smoothstep(lava_radius * 3.0, 0.0, h);
			vec3 emit = vec3(1.0, 0.38, 0.08) * lava * dens;
			float a = 1.0 - exp(-dens * extinction * dt);
			if (t > t_plane - mt_front) a *= 1.0 - mt_cover;   // derriere la montagne (marge : la colonne est a cheval sur son plan)
			col += T * (tint * light + emit) * a;
			T *= 1.0 - a;
			if (T < 0.015) break;
		}
	}
	float alpha = 1.0 - T;
	// brume atmospherique : le panache lointain se fond dans l'horizon
	float dist = length(origin - cam);
	float haze = 1.0 - exp(-dist * haze_density);
	// perspective atmospherique : le panache est voile comme la montagne (sinon il parait au premier plan)
	haze = max(haze, haze_floor);
	vec3 aerial = mix(fog_color, aerial_tint * (0.03 + 0.97 * day_light), 0.5);
	col = mix(col, aerial * alpha, haze * 0.95);
	ALBEDO = col * fade;
	ALPHA = alpha * fade;
}
