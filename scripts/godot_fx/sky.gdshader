shader_type sky;
// Ciel de Morrowind : degrade atmospherique (Rayleigh/Mie simplifies), disque solaire, deux lunes
// (Masser, Secunda) eclairees par le soleil, voie lactee, nuages. Parametres pilotes par daynight.gd.
// Equivalent Godot de l'effet "VAIO" (ciel + nuages + astres) de Rafael's Shader Pack.

uniform vec3 sun_dir = vec3(0.3, 0.8, 0.5);        // direction VERS le soleil
uniform vec3 masser_dir = vec3(-0.5, 0.6, 0.2);
uniform vec3 secunda_dir = vec3(0.4, 0.5, -0.6);
uniform float masser_radius = 0.05;               // rayon angulaire (radians)
uniform float secunda_radius = 0.032;

uniform float cloud_density : hint_range(0.0, 1.0) = 0.43;
uniform float cloud_min : hint_range(0.0, 1.0) = 0.33;
uniform float cloud_speed = 0.004;
uniform float cloud_scale = 0.55;
uniform vec3 cloud_tint = vec3(1.0);
uniform float overcast : hint_range(0.0, 1.0) = 0.0;   // pluie : ciel gris et couvert

uniform float star_brightness = 0.5;
uniform float twinkle = 1.0;
uniform float mie_g = 0.5;

uniform sampler2D cloud_noise : repeat_enable, filter_linear_mipmap, hint_default_white;
uniform sampler2D star_map : source_color, repeat_enable, filter_linear_mipmap, hint_default_black;
uniform sampler2D masser_tex : source_color, repeat_enable, filter_linear, hint_default_white;
uniform sampler2D secunda_tex : source_color, repeat_enable, filter_linear, hint_default_white;

float hg_phase(float cos_t, float g) {
	float g2 = g * g;
	return (1.0 - g2) / (4.0 * PI * pow(1.0 + g2 - 2.0 * g * cos_t, 1.5));
}

float hash12(vec2 p) {
	vec3 p3 = fract(vec3(p.xyx) * 0.1031);
	p3 += dot(p3, p3.yzx + 33.33);
	return fract((p3.x + p3.y) * p3.z);
}

float fbm(vec2 p) {
	float a = 0.5, s = 0.0;
	for (int i = 0; i < 4; i++) {
		s += a * texture(cloud_noise, p).r;
		p = p * 2.03 + vec2(0.37, 0.11);
		a *= 0.5;
	}
	return s / 0.9375;
}

// Disque lunaire : couleur eclairee (phase) + opacite
vec4 moon(vec3 dir, vec3 m, float radius, sampler2D tex, float night, float gain) {
	float c = dot(dir, m);
	if (c < cos(radius * 1.2)) return vec4(0.0);
	vec3 t = normalize(cross(vec3(0.0, 1.0, 0.0), m));
	vec3 b = cross(m, t);
	vec2 q = vec2(dot(dir, t), dot(dir, b)) / sin(radius);   // -1..1 sur le disque
	float r2 = dot(q, q);
	float edge = smoothstep(1.02, 0.96, sqrt(r2));
	if (edge <= 0.0) return vec4(0.0);
	float z = sqrt(max(1.0 - r2, 0.0));
	vec3 n = normalize(t * q.x + b * q.y + m * z);            // normale de la sphere (monde)
	vec2 uv = vec2(atan(q.x, z) / (2.0 * PI) + 0.5, asin(clamp(q.y, -1.0, 1.0)) / PI + 0.5);
	vec3 albedo = texture(tex, uv).rgb;
	float lit = smoothstep(-0.12, 0.55, dot(n, sun_dir));           // phase de la lune
	vec3 col = albedo * gain * (lit * 1.8 + 0.07);                  // + lumiere cendree : la face sombre reste visible
	return vec4(col, edge * clamp(night * 1.4 + 0.15, 0.0, 1.0));
}

void sky() {
	vec3 dir = EYEDIR;
	float sun_h = sun_dir.y;
	float day = smoothstep(-0.12, 0.22, sun_h);                    // 0 nuit .. 1 jour
	float night = 1.0 - smoothstep(-0.25, 0.05, sun_h);
	float twilight = clamp(1.0 - abs(sun_h) * 4.0, 0.0, 1.0);

	// ---- ciel : degrade zenith -> horizon, plus chaud pres du soleil au crepuscule ----
	vec3 zenith_day = vec3(0.13, 0.30, 0.68);
	vec3 horizon_day = vec3(0.58, 0.72, 0.88);
	vec3 zenith_night = vec3(0.004, 0.008, 0.025);
	vec3 horizon_night = vec3(0.015, 0.025, 0.05);
	vec3 zenith = mix(zenith_night, zenith_day, day);
	vec3 horizon = mix(horizon_night, horizon_day, day);
	vec3 dusk = vec3(1.0, 0.45, 0.2);
	float h = clamp(dir.y, 0.0, 1.0);
	float grad = pow(1.0 - h, 3.2);
	vec3 col = mix(zenith, horizon, grad);
	float toward_sun = max(dot(normalize(vec3(dir.x, 0.0, dir.z) + 1e-5), normalize(vec3(sun_dir.x, 0.0, sun_dir.z) + 1e-5)), 0.0);
	col = mix(col, dusk * 0.9, twilight * pow(toward_sun, 3.0) * pow(1.0 - h, 2.0) * 0.85);
	// diffusion de Mie autour du soleil
	float cos_s = dot(dir, sun_dir);
	float mie = hg_phase(cos_s, mie_g) * 0.25;
	col += vec3(1.0, 0.85, 0.65) * mie * day * (1.0 - overcast * 0.7);
	// sous l'horizon : sombre
	// (meme couleur que le brouillard de l'horizon : pas de bande sombre ou le terrain s'arrete)
	col = mix(col, horizon * mix(1.0, 0.55, smoothstep(-0.05, -0.7, dir.y)), smoothstep(0.0, -0.04, dir.y));

	// ---- voie lactee / etoiles ----
	if (dir.y > -0.05 && night > 0.0) {
		vec2 suv = vec2(atan(dir.x, dir.z) / (2.0 * PI) + 0.5, acos(clamp(dir.y, -1.0, 1.0)) / PI);
		// couture de atan() : on prend la parametrisation decalee de 0.5 la ou elle est continue
		vec2 suv2 = vec2(fract(suv.x + 0.5), suv.y);
		vec2 gx = dFdx(suv), gy = dFdy(suv), gx2 = dFdx(suv2), gy2 = dFdy(suv2);
		bool ok = dot(gx, gx) + dot(gy, gy) <= dot(gx2, gx2) + dot(gy2, gy2);
		vec3 stars = textureGrad(star_map, suv, ok ? gx : gx2, ok ? gy : gy2).rgb;
		float tw = 1.0 + twinkle * 0.35 * (hash12(floor(suv * 1500.0)) - 0.5) * sin(TIME * 3.0 + hash12(floor(suv * 700.0)) * 40.0);
		col += stars * star_brightness * 2.2 * night * tw * (1.0 - overcast);
	}

	// ---- lunes ----
	vec4 mm = moon(dir, masser_dir, masser_radius, masser_tex, night, 3.2);
	col = mix(col, mm.rgb, mm.a * (1.0 - overcast * 0.9));
	vec4 sm = moon(dir, secunda_dir, secunda_radius, secunda_tex, night, 1.0);
	col = mix(col, sm.rgb, sm.a * (1.0 - overcast * 0.9));

	// ---- soleil ----
	float sun_disc = smoothstep(0.99985, 0.99992, cos_s);
	float sun_glow = pow(max(cos_s, 0.0), 220.0) * 0.8 + pow(max(cos_s, 0.0), 12.0) * 0.12;
	vec3 sun_col = mix(vec3(1.0, 0.55, 0.25), vec3(1.0, 0.96, 0.85), clamp(sun_h * 3.0, 0.0, 1.0));
	if (sun_h > -0.06) {
		float vis = smoothstep(-0.06, 0.02, sun_h);
		col += sun_col * (sun_disc * 14.0 + sun_glow) * vis * (1.0 - overcast * 0.85);
	}

	// ---- nuages : plan projete, FBM de bruit, eclaires par le soleil ----
	if (dir.y > 0.015) {
		vec2 p = dir.xz / (dir.y + 0.18) * cloud_scale * 0.35 + vec2(TIME * cloud_speed, TIME * cloud_speed * 0.4);
		float n = fbm(p);
		float cover = clamp(cloud_density + overcast * 0.55, 0.0, 1.0);
		float c = smoothstep(cloud_min * (1.0 - cover) + 0.15, cloud_min * (1.0 - cover) + 0.62, n * (0.55 + cover));
		c *= smoothstep(0.015, 0.22, dir.y);                         // fondu vers l'horizon
		// eclairage : bord vers le soleil plus clair, dessous plus sombre
		float n2 = fbm(p + sun_dir.xz * 0.05);
		float shade = clamp(0.55 + (n - n2) * 5.0, 0.2, 1.0);
		vec3 lit = mix(vec3(0.55, 0.60, 0.68), vec3(1.0, 0.98, 0.95), shade) * cloud_tint;
		lit = mix(lit, dusk * 1.05, twilight * 0.55);
		lit = mix(lit, vec3(0.45, 0.48, 0.52), overcast * 0.7);
		lit *= mix(0.05, 1.0, day);                                 // la nuit : nuages sombres
		lit += vec3(1.0, 0.9, 0.7) * pow(max(cos_s, 0.0), 6.0) * 0.45 * day * (1.0 - c * 0.3);   // liseré
		col = mix(col, lit, c * 0.92);
	}

	COLOR = col;
}
