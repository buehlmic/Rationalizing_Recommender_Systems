MKL_NUM_THREADS=1 python rrs.py \
--linkdata_pos "../../../Data/Processed_Data/Kindle/D.pkl" \
--linkdata_neg "../../../Data/Processed_Data/Kindle/D_C.pkl" \
--flic "../../../Data/Processed_Data/Kindle/FLIC_model_attdim200_nsteps1000.pkl" \
--reviews "../../../Data/Processed_Data/Kindle/reviews_embedded_256.pkl" \
--save_model "../../../Data/Processed_Data/Kindle/Models/" \
--training_output_path "../../../Data/Processed_Data/Kindle/Output/" \
--enc_num_hidden_units 256 \
--enc_num_hidden_layers 1 \
--gen_num_hidden_units 256 \
--gen_num_hidden_layers 1 \
--max_epochs 100 \
--num_samples 10 \
--num_target_sentences 3 \
--category Kindle \
--measure_timing \
--adaptive_lrs \
--regularizer 0.005 \
--model_type DPP \
--context train_context \
--num_cross_validation 5 \
